# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Phone calls to the managers of a location when a person alarm is raised.

Each location lists its managers (a party and one of its phone numbers). When an alarm
for a person (detected or moving) is raised on a camera of that location, the Yeastar PBX
configured in the videovigilance configuration (module yeastar_api) is asked to call every
manager. The calls are recorded in the alarm and are placed after the transaction that
raised the alarm has been committed, so a PBX problem can never lose the event or the alarm.
"""
import json
import logging
from datetime import datetime, timedelta

import requests
from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.model import ModelSQL, ModelView, fields
from trytond.model.exceptions import ValidationError
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval
from trytond.transaction import Transaction, without_check_access

from .event import PERSON_TYPES

logger = logging.getLogger(__name__)

PHONE_TYPES = ['phone', 'mobile']
ERROR_MAX = 500
VOICE_REPEATS = 2
TTS_TIMEOUT_S = 15
HTTP_TIMEOUT_S = 15
# yeastar_api sends this on every request; the PBX identifies the application by it.
HEADERS = {'Content-Type': 'application/json', 'User-Agent': 'OpenAPI'}


def dialable(mechanism):
    "The number of a contact mechanism as the PBX expects it, or None."
    if mechanism is None:
        return None
    Contact = Pool().get('yeastar.contact')
    return Contact.valid_number(mechanism.value_compact or mechanism.value)


def prompt_name(location):
    "The PBX custom prompt of a location: reused (overwritten) by each alarm, so they never pile up."
    return 'videovigilancia_%d' % location.id


def alarm_text(alarm):
    "What is read to the manager."
    from .push import local_time
    camera = alarm.camera
    text = 'Alarma de videovigilancia. %s en la cámara %s, ubicación %s, a las %s.' % (
        alarm.title, camera.name, camera.location.name,
        local_time(alarm.raised_at, '%H:%M'))
    persons = alarm.event.max_persons if alarm.event else None
    if persons:
        text += ' %d persona%s.' % (persons, '' if persons == 1 else 's')
    return text


def synthesize(text, voice):
    """The text as speech, in the format the PBX takes: PCM 16 bit, 8 kHz WAV.

    Google Text-to-Speech, like yeastar_api's voice prompts (it needs Google credentials,
    GOOGLE_APPLICATION_CREDENTIALS, in trytond's environment).
    """
    try:
        import google.cloud.texttospeech as tts
    except ImportError:
        raise RuntimeError("Falta la biblioteca google-cloud-texttospeech") from None
    if not voice:
        raise RuntimeError("Falta la veu a la configuració")
    client = tts.TextToSpeechClient()
    response = client.synthesize_speech(
        input=tts.SynthesisInput(text=text),
        voice=tts.VoiceSelectionParams(
            language_code='-'.join(voice.split('-')[:2]), name=voice),
        audio_config=tts.AudioConfig(
            audio_encoding=tts.AudioEncoding.LINEAR16, sample_rate_hertz=8000),
        timeout=TTS_TIMEOUT_S)
    return response.audio_content


class YeastarPBX(metaclass=PoolMeta):
    __name__ = 'yeastar.pbx'

    def upload_custom_prompt(self, name, wav):
        """Upload (overwriting) a custom prompt.

        https://help.yeastar.com/en/p-series-cloud-edition/developer-guide/upload-a-custom-prompt.html
        """
        token = self.get_token()
        url = self.construct_url('custom_prompt/upload', token=token)
        response = requests.post(
            url, files={'file': ('%s.wav' % name, wav, 'audio/wav')},
            data={'is_check': 'force'}, headers={'User-Agent': HEADERS['User-Agent']},
            verify=False, timeout=HTTP_TIMEOUT_S)
        result = response.json() if response.status_code == 200 else {}
        if result.get('errcode', 1) != 0:
            raise UserError(gettext(
                    'videovigilance.msg_prompt_upload_failed', pbx=self.rec_name,
                    errmsg='HTTP %s %s' % (response.status_code, result.get('errmsg', ''))))

    def play_prompt(self, number, prompts, dial_permission=None, count=1):
        """Call a number and play custom prompts to it. Returns the call id.

        https://help.yeastar.com/en/p-series-cloud-edition/developer-guide/play-prompt.html
        """
        token = self.get_token()
        data = {'number': number, 'prompts': list(prompts), 'count': count}
        if dial_permission:
            data['dial_permission'] = dial_permission
        result = self._requests(
            'POST', 'call/play_prompt', data=json.dumps(data), headers=HEADERS,
            token=token)
        if not result or result.get('errcode', 1) != 0:
            raise UserError(gettext(
                    'videovigilance.msg_play_prompt_failed', pbx=self.rec_name,
                    errmsg=result.get('errmsg', '') if result else ''))
        return result.get('call_id')


class LocationManager(ModelSQL, ModelView):
    "Encarregat d'una ubicació de videovigilància"
    __name__ = 'videovigilance.location.manager'

    location = fields.Many2One(
        'videovigilance.location', "Ubicació", required=True, ondelete='CASCADE')
    sequence = fields.Integer("Seqüència")
    party = fields.Many2One(
        'party.party', "Encarregat", required=True, ondelete='RESTRICT')
    phone = fields.Many2One(
        'party.contact_mechanism', "Telèfon", required=True, ondelete='RESTRICT',
        domain=[
            ('party', '=', Eval('party', -1)),
            ('type', 'in', PHONE_TYPES),
            ],
        help="Número al qual es truca. Només xifres (el + es converteix en 00).")

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order = [('sequence', 'ASC'), ('id', 'ASC')]

    @classmethod
    def validate(cls, managers):
        super().validate(managers)
        for manager in managers:
            if not dialable(manager.phone):
                raise ValidationError(gettext(
                        'videovigilance.msg_manager_number_invalid',
                        party=manager.party.rec_name,
                        number=manager.phone.value if manager.phone else ''))

    @fields.depends('party', 'phone')
    def on_change_party(self):
        if self.phone is not None and self.phone.party != self.party:
            self.phone = None
        if self.phone is None and self.party:
            for mechanism in self.party.contact_mechanisms:
                if mechanism.type in PHONE_TYPES:
                    self.phone = mechanism
                    break


class Call(ModelSQL, ModelView):
    "Trucada d'una alarma de videovigilància"
    __name__ = 'videovigilance.alarm.call'

    alarm = fields.Many2One(
        'videovigilance.alarm', "Alarma", required=True, readonly=True,
        ondelete='CASCADE')
    location = fields.Many2One(
        'videovigilance.location', "Ubicació", readonly=True, ondelete='SET NULL')
    party = fields.Many2One(
        'party.party', "Encarregat", readonly=True, ondelete='SET NULL')
    number = fields.Char("Número", required=True, readonly=True)
    raised_at = fields.DateTime("Data", required=True, readonly=True)
    state = fields.Selection(
        [('pending', "Pendent"), ('placed', "Iniciada"), ('failed', "Fallida")],
        "Estat", required=True, readonly=True,
        help="«Iniciada» vol dir que la centraleta ha acceptat la ordre de "
        "trucar, no que l'encarregat hagi respost.")
    call_id = fields.Char("Identificador de la trucada", readonly=True)
    error = fields.Char("Error", readonly=True)
    voice = fields.Boolean(
        "Amb veu", readonly=True,
        help="La centraleta ha llegit l'alarma a l'encarregat. Si no, només ha sonat.")
    voice_error = fields.Char(
        "Error de la veu", readonly=True,
        help="Per què no s'ha llegit l'alarma. La trucada s'ha fet igualment.")

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order = [('raised_at', 'DESC'), ('id', 'DESC')]

    @staticmethod
    def default_state():
        return 'pending'

    @classmethod
    def request_for(cls, alarms):
        """Record the calls that the alarms require and queue them.

        At most one round of calls per location (also among simultaneous alarms, such
        as a person who is detected and moving at once) within the cooldown.
        """
        Configuration = Pool().get('videovigilance.configuration')
        with without_check_access():
            configuration = Configuration.get_singleton()
            if (configuration is None or not configuration.yeastar_pbx
                    or not configuration.yeastar_caller):
                return []
            now = datetime.now()
            since = now - timedelta(seconds=configuration.call_cooldown)
            values, done = [], set()
            for alarm in alarms:
                location = alarm.camera.location if alarm.camera else None
                if (alarm.type not in PERSON_TYPES or location is None
                        or location.id in done or not location.managers):
                    continue
                if configuration.call_cooldown and cls.search_count([
                            ('location', '=', location.id),
                            ('state', '!=', 'failed'),
                            ('raised_at', '>=', since),
                            ]):
                    continue
                done.add(location.id)
                for manager in location.managers:
                    values.append({
                            'alarm': alarm.id, 'location': location.id,
                            'party': manager.party.id,
                            'number': dialable(manager.phone),
                            'raised_at': now,
                            })
            if not values:
                return []
            calls = cls.create(values)
        cls.__queue__.place(calls)
        return calls

    @staticmethod
    def _text(error):
        # A Char field cannot hold line breaks, and the PBX messages have them.
        return ' '.join((str(error) or repr(error)).split())[:ERROR_MAX]

    @classmethod
    def _prepare_voice(cls, configuration, call):
        """Upload the spoken alarm to the PBX. Returns (prompt name or None, error text).

        Never raises: without a voice message the managers are still called.
        """
        if not configuration.call_voice:
            return None, None
        try:
            name = prompt_name(call.location)
            wav = synthesize(alarm_text(call.alarm), configuration.call_voice_name)
            configuration.yeastar_pbx.upload_custom_prompt(name, wav)
            return name, None
        except Exception as error:
            logger.exception("No s'ha pogut preparar la veu de l'alarma %s", call.alarm.id)
            return None, cls._text(error)

    @classmethod
    def _ring(cls, configuration, call, voice, voice_error):
        """Place one call: the PBX reads the alarm if there is a voice for it, and otherwise
        (or if it cannot) just rings. Returns the values that say how it went; never raises."""
        pbx = configuration.yeastar_pbx
        values = {'voice': False}
        try:
            call_id = None
            if voice:
                try:
                    call_id = pbx.play_prompt(
                        call.number, [voice],
                        dial_permission=configuration.yeastar_dial_permission,
                        count=VOICE_REPEATS)
                    values['voice'] = True
                except Exception as error:
                    logger.exception("No s'ha pogut reproduir la veu al %s", call.number)
                    voice_error = cls._text(error)
            if call_id is None:
                call_id = pbx.make_call(configuration.yeastar_caller, call.number)
            values.update(state='placed', call_id=call_id)
        except Exception as error:
            logger.exception(
                "No s'ha pogut trucar al %s (alarma %s)", call.number, call.alarm.id)
            values.update(state='failed', error=cls._text(error))
        values['voice_error'] = voice_error
        return values

    @classmethod
    def place(cls, calls):
        """Ask the PBX to call each pending call's number, recording how it went.

        With voice, the PBX calls and reads the alarm (a custom prompt uploaded once per
        alarm); if the voice cannot be prepared or played, it simply rings.
        """
        Configuration = Pool().get('videovigilance.configuration')
        pending = [call for call in calls if call.state == 'pending']
        with without_check_access(), Transaction().set_context(yeastar_wo_employee=True):
            configuration = Configuration.get_singleton()
            if not (configuration and configuration.yeastar_pbx
                    and configuration.yeastar_caller):
                # It was set up when the alarm was raised and has been unset since.
                cls.write(pending, {
                        'state': 'failed',
                        'error': "Falta la centraleta o el número d'origen a la configuració"})
                return
            voices = {}
            for call in pending:
                if call.alarm.id not in voices:
                    voices[call.alarm.id] = cls._prepare_voice(configuration, call)
                cls.write([call], cls._ring(configuration, call, *voices[call.alarm.id]))
                # Each result is kept even if a later call fails.
                Transaction().commit()
