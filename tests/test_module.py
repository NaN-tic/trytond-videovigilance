# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.tests.test_tryton import ModuleTestCase, with_transaction


class VideovigilanceTestCase(ModuleTestCase):
    'Test Videovigilance module'
    module = 'videovigilance'

    @with_transaction(user=0)
    def test_wizards(self):
        # The shared framework check must also inspect group-restricted wizards.
        return super().test_wizards.__wrapped__(self)

del ModuleTestCase
