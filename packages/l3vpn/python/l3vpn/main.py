# -*- mode: python; python-indent: 4 -*-
import ipaddress

import ncs
from ncs.application import Service


def prefix_to_mask(prefix_length):
    return str(ipaddress.IPv4Network(f'0.0.0.0/{prefix_length}').netmask)


class ServiceCallbacks(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')

        tvars = ncs.template.Variables()
        tvars.add('PE1_MASK', prefix_to_mask(service.pe1.prefix_length))
        tvars.add('PE2_MASK', prefix_to_mask(service.pe2.prefix_length))
        template = ncs.template.Template(service)
        template.apply('l3vpn-template', tvars)


class Main(ncs.application.Application):
    def setup(self):
        self.log.info('Main RUNNING')
        self.register_service('l3vpn-servicepoint', ServiceCallbacks)

    def teardown(self):
        self.log.info('Main FINISHED')
