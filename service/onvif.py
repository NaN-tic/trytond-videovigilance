"""Minimal ONVIF client for pan/tilt control (own implementation, no SOAP library).

It speaks the few SOAP calls needed to move a camera: it finds the PTZ and media
services, picks a profile, and sends continuous moves, stops, and presets. Requests are
authenticated with WS-Security UsernameToken digest, using the camera's own clock so that
a skewed clock does not make the camera reject them.
"""
import base64
import hashlib
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import requests

NS = {
    's': 'http://www.w3.org/2003/05/soap-envelope',
    'tds': 'http://www.onvif.org/ver10/device/wsdl',
    'trt': 'http://www.onvif.org/ver10/media/wsdl',
    'tptz': 'http://www.onvif.org/ver20/ptz/wsdl',
    'tt': 'http://www.onvif.org/ver10/schema',
}
WSSE = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd'
WSU = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd'
PASSWORD_DIGEST = ('http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-'
    'username-token-profile-1.0#PasswordDigest')
BASE64 = ('http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-'
    'message-security-1.0#Base64Binary')


class OnvifError(Exception):
    "The camera is unreachable, rejected the request, or answered something unexpected."


def _text(value):
    return escape(str(value))


def axes_for_rotation(dx, dy, rotation):
    """Camera pan/tilt for a direction as seen on the (rotated) picture.

    dx is to the right and dy is up on screen, each from -1 to 1. `rotation` is how many
    degrees (0, 90, 180, 270) the picture is turned clockwise so that it looks upright,
    which is also how the camera body is turned relative to the scene. A camera mounted
    upside down needs both axes inverted for «right» to look right.
    """
    if rotation == 0:
        return dx, dy
    if rotation == 90:
        return -dy, dx
    if rotation == 180:
        return -dx, -dy
    if rotation == 270:
        return dy, -dx
    raise ValueError('rotation must be 0, 90, 180 or 270')


def _speed(value):
    "A speed component as ONVIF expects it: a number between -1 and 1."
    return '%.3f' % max(-1.0, min(1.0, float(value)))


class OnvifCamera:
    """One camera. Thread-safe: calls are short and the discovered state is cached."""

    def __init__(self, host, port, username, password, timeout=6):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._clock_offset = None
        self._services = None
        self._profile = None

    @property
    def _device_url(self):
        host = f'[{self._host}]' if ':' in self._host else self._host
        return f'http://{host}:{self._port}/onvif/device_service'

    def _created(self):
        "The camera's current UTC time (measured once), as WS-Security wants it."
        if self._clock_offset is None:
            self._clock_offset = self._measure_offset()
        moment = datetime.now(timezone.utc) + self._clock_offset
        return moment.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    def _measure_offset(self):
        body = '<tds:GetSystemDateAndTime xmlns:tds="%s"/>' % NS['tds']
        try:
            root = self._post(self._device_url, body, secure=False)
            utc = root.find('.//tt:UTCDateTime', NS)
            date, clock = utc.find('tt:Date', NS), utc.find('tt:Time', NS)
            camera = datetime(
                int(date.find('tt:Year', NS).text), int(date.find('tt:Month', NS).text),
                int(date.find('tt:Day', NS).text), int(clock.find('tt:Hour', NS).text),
                int(clock.find('tt:Minute', NS).text),
                int(clock.find('tt:Second', NS).text), tzinfo=timezone.utc)
        except (OnvifError, AttributeError, ValueError):
            return timedelta(0)  # the camera did not tell: assume the clocks agree
        return camera - datetime.now(timezone.utc)

    def _header(self):
        nonce = os.urandom(16)
        created = self._created()
        digest = base64.b64encode(hashlib.sha1(
                nonce + created.encode() + self._password.encode()).digest()).decode()
        return (
            '<s:Header><Security xmlns="%s" s:mustUnderstand="1"><UsernameToken>'
            '<Username>%s</Username><Password Type="%s">%s</Password>'
            '<Nonce EncodingType="%s">%s</Nonce><Created xmlns="%s">%s</Created>'
            '</UsernameToken></Security></s:Header>' % (
                WSSE, _text(self._username), PASSWORD_DIGEST, digest, BASE64,
                base64.b64encode(nonce).decode(), WSU, created))

    def _post(self, url, body, secure=True):
        envelope = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="%s">%s'
            '<s:Body>%s</s:Body></s:Envelope>' % (
                NS['s'], self._header() if secure else '', body))
        try:
            response = self._session.post(url, data=envelope.encode(),
                headers={'Content-Type': 'application/soap+xml; charset=utf-8'},
                timeout=self._timeout)
        except requests.RequestException as error:
            raise OnvifError(f'la cámara no responde ({type(error).__name__})') from None
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError:
            raise OnvifError(f'respuesta no válida (HTTP {response.status_code})') from None
        fault = root.find('.//s:Fault', NS)
        if fault is not None:
            reason = ' '.join(''.join(t.itertext()).strip() for t in fault.iter()
                if t.tag.endswith('}Text'))[:160]
            raise OnvifError(reason or f'la cámara rechazó la petición (HTTP {response.status_code})')
        if response.status_code >= 400:
            raise OnvifError(f'HTTP {response.status_code}')
        return root

    def call(self, service, body):
        "Run an operation on the 'device', 'media' or 'ptz' service and return the XML."
        return self._post(self._service_urls()[service], body)

    def _service_urls(self):
        if self._services is None:
            root = self._post(self._device_url,
                '<tds:GetCapabilities xmlns:tds="%s"><tds:Category>All</tds:Category>'
                '</tds:GetCapabilities>' % NS['tds'])
            found = {}
            for name, key in (('Media', 'media'), ('PTZ', 'ptz')):
                node = root.find('.//tt:%s/tt:XAddr' % name, NS)
                if node is not None and node.text:
                    found[key] = self._same_host(node.text)
            found['device'] = self._device_url
            self._services = found
        return self._services

    def _same_host(self, address):
        "Cameras sometimes announce an internal address; always go through the known host."
        match = re.match(r'(https?://)[^/]+(/.*)', address)
        host = f'[{self._host}]' if ':' in self._host else self._host
        return f'{match.group(1)}{host}:{self._port}{match.group(2)}' if match else address

    def _profile_token(self):
        if self._profile is None:
            if 'media' not in self._service_urls():
                raise OnvifError('la cámara no ofrece servicio de vídeo ONVIF')
            root = self.call('media', '<trt:GetProfiles xmlns:trt="%s"/>' % NS['trt'])
            profiles = root.findall('.//trt:Profiles', NS)
            # The first profile that has a PTZ configuration is the one that can move.
            for profile in profiles:
                if profile.find('tt:PTZConfiguration', NS) is not None:
                    self._profile = profile.get('token')
                    break
            else:
                if not profiles:
                    raise OnvifError('la cámara no tiene perfiles ONVIF')
                self._profile = profiles[0].get('token')
        return self._profile

    def device_info(self):
        """What the camera says about itself: manufacturer, model, firmware, serial and
        hardware (the ones it does not report are left out). Needs the camera's login."""
        root = self._post(self._device_url,
            '<tds:GetDeviceInformation xmlns:tds="%s"/>' % NS['tds'])
        info = {}
        for key, tag in (('manufacturer', 'Manufacturer'), ('model', 'Model'),
                ('firmware', 'FirmwareVersion'), ('serial', 'SerialNumber'),
                ('hardware', 'HardwareId')):
            value = (root.findtext('.//tds:%s' % tag, default='', namespaces=NS) or '').strip()
            if value:
                info[key] = value
        if not info:
            raise OnvifError('la cámara no informa de su marca ni modelo')
        return info

    def supports_ptz(self):
        return 'ptz' in self._service_urls()

    def _ptz(self, operation, inner=''):
        if not self.supports_ptz():
            raise OnvifError('la cámara no ofrece movimiento ONVIF')
        body = '<tptz:%s xmlns:tptz="%s" xmlns:tt="%s"><tptz:ProfileToken>%s' \
            '</tptz:ProfileToken>%s</tptz:%s>' % (
                operation, NS['tptz'], NS['tt'], _text(self._profile_token()), inner, operation)
        return self.call('ptz', body)

    def move(self, pan, tilt, seconds=None):
        """Start moving at the given speeds (-1 to 1 per axis).

        The camera keeps moving until `stop()` is called, or until `seconds` pass when
        given (the camera stops by itself, so a lost stop cannot leave it running).
        The camera only accepts whole seconds (it drops the connection on «PT2.0S»).
        """
        timeout = ('<tptz:Timeout>PT%dS</tptz:Timeout>' % max(1, round(seconds))
            if seconds else '')
        self._ptz('ContinuousMove',
            '<tptz:Velocity><tt:PanTilt x="%s" y="%s"/></tptz:Velocity>%s' % (
                _speed(pan), _speed(tilt), timeout))

    def stop(self):
        self._ptz('Stop', '<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>')

    def presets(self):
        "Saved positions as [(token, name)]."
        root = self._ptz('GetPresets')
        return [(node.get('token'), (node.findtext('tt:Name', default='', namespaces=NS)
                    or node.get('token')))
            for node in root.findall('.//tptz:Preset', NS)]

    def go_to_preset(self, token):
        self._ptz('GotoPreset', '<tptz:PresetToken>%s</tptz:PresetToken>' % _text(token))

    def position(self):
        "Current (pan, tilt) between -1 and 1, or None if the camera does not report it."
        root = self._ptz('GetStatus')
        node = root.find('.//tt:Position/tt:PanTilt', NS)
        if node is None:
            return None
        return float(node.get('x')), float(node.get('y'))
