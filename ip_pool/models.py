from datetime import timedelta
from ipaddress import ip_network, ip_address
from typing import AnyStr

from django.conf import settings
from django.db.utils import IntegrityError
from django.shortcuts import resolve_url
from django.core.exceptions import ValidationError, ImproperlyConfigured
from django.db import models
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from djing.lib import DuplicateEntry
from ip_pool.fields import GenericIpAddressWithPrefix
from group_app.models import Group


class NetworkModel(models.Model):
    _netw_cache = None

    network = GenericIpAddressWithPrefix(
        verbose_name=_('IP network'),
        help_text=_('Ip address of network. For example: 192.168.1.0 or fde8:6789:1234:1::'),
        unique=True
    )
    NETWORK_KINDS = (
        ('inet', _('Internet')),
        ('guest', _('Guest')),
        ('trust', _('Trusted')),
        ('device', _('Devices')),
        ('admin', _('Admin'))
    )
    kind = models.CharField(_('Kind of network'), max_length=6, choices=NETWORK_KINDS, default='guest')
    description = models.CharField(_('Description'), max_length=64)
    groups = models.ManyToManyField(Group, verbose_name=_('Groups'))

    # Usable ip range
    ip_start = models.GenericIPAddressField(_('Start work ip range'))
    ip_end = models.GenericIPAddressField(_('End work ip range'))

    def __str__(self):
        netw = self.get_network()
        return "%s: %s" % (self.description, netw.with_prefixlen)

    def get_network(self):
        if self.network is None:
            return
        if self._netw_cache is None:
            self._netw_cache = ip_network(self.network)
        return self._netw_cache

    def get_absolute_url(self):
        return resolve_url('ip_pool:net_edit', self.pk)

    def clean(self):
        errs = {}
        if self.network is None:
            errs['network'] = ValidationError(_('Network is invalid'), code='invalid')
            raise ValidationError(errs)
        net = self.get_network()
        if self.ip_start is None:
            errs['ip_start'] = ValidationError(_('Ip start is invalid'), code='invalid')
            raise ValidationError(errs)
        start_ip = ip_address(self.ip_start)
        if start_ip not in net:
            errs['ip_start'] = ValidationError(_('Start ip must be in subnet of specified network'), code='invalid')
        if self.ip_end is None:
            errs['ip_end'] = ValidationError(_('Ip end is invalid'), code='invalid')
            raise ValidationError(errs)
        end_ip = ip_address(self.ip_end)
        if end_ip not in net:
            errs['ip_end'] = ValidationError(_('End ip must be in subnet of specified network'), code='invalid')
        if errs:
            raise ValidationError(errs)

        other_nets = NetworkModel.objects.exclude(pk=self.pk).only('network').order_by('network')
        if not other_nets.exists():
            return
        for onet in other_nets.iterator():
            onet_netw = onet.get_network()
            if net.overlaps(onet_netw):
                errs['network'] = ValidationError(_('Network is overlaps with %(other_network)s'), params={
                    'other_network': str(onet_netw)
                })
                raise ValidationError(errs)

    def get_scope(self) -> AnyStr:
        net = self.get_network()
        if net.is_global:
            return _('Global')
        elif net.is_link_local:
            return _('Link local')
        elif net.is_loopback:
            return _('Loopback')
        elif net.is_multicast:
            return _('Multicast')
        elif net.is_private:
            return _('Private')
        elif net.is_reserved:
            return _('Reserved')
        elif net.is_site_local:
            return _('Site local')
        elif net.is_unspecified:
            return _('Unspecified')
        return "I don't know"

    class Meta:
        db_table = 'ip_pool_network'
        verbose_name = _('Network')
        verbose_name_plural = _('Networks')
        ordering = ('network',)


class IpLeaseManager(models.Manager):

    def get_free_ip(self, network: NetworkModel):
        netw = network.get_network()
        work_range_start_ip = ip_address(network.ip_start)
        work_range_end_ip = ip_address(network.ip_end)
        employed_ip_queryset = self.filter(network=network, is_dynamic=False).order_by('ip').only('ip')

        if employed_ip_queryset.exists():
            used_ip_gen = employed_ip_queryset.iterator()
            for net_ip in netw.hosts():
                if net_ip < work_range_start_ip:
                    continue
                elif net_ip > work_range_end_ip:
                    break
                used_ip = next(used_ip_gen, None)
                if used_ip is None:
                    return net_ip
                ip = ip_address(used_ip.ip)
                if net_ip < ip:
                    return net_ip
        else:
            for net in netw.hosts():
                if work_range_start_ip <= net <= work_range_end_ip:
                    return net

    def create_from_ip(self, ip: str, net: NetworkModel, is_dynamic=True):
        # ip = ip_address(ip)
        try:
            return self.create(
                ip=ip,
                network=net,
                is_dynamic=is_dynamic,
                is_active=True
            )
        except IntegrityError as e:
            if 'Duplicate entry' in str(e):
                raise DuplicateEntry(_('Ip has already taken'))
            raise e

    def expired(self):
        lease_live_time = getattr(settings, 'LEASE_LIVE_TIME')
        if lease_live_time is None:
            raise ImproperlyConfigured('You must specify LEASE_LIVE_TIME in settings')
        senility = now() - timedelta(seconds=lease_live_time)
        return self.filter(lease_time__lt=senility, is_active=False)


class IpLeaseModel(models.Model):
    ip = models.GenericIPAddressField(verbose_name=_('Ip address'), unique=True)
    network = models.ForeignKey(NetworkModel, on_delete=models.CASCADE, verbose_name=_('Parent network'))
    lease_time = models.DateTimeField(_('Lease time'), auto_now_add=True)
    is_dynamic = models.BooleanField(_('Is dynamic'), default=False)
    is_active = models.BooleanField(_('Is active'), default=True)

    objects = IpLeaseManager()

    def __str__(self):
        return self.ip

    def free(self):
        if self.is_active:
            self.is_active = False
            self.save(update_fields=('is_active',))

    def start(self):
        if not self.is_active:
            self.is_active = True
            self.save(update_fields=('is_active',))

    def clean(self):
        ip = ip_address(self.ip)
        network = self.network.get_network()
        if ip not in network:
            raise ValidationError(_('Ip address %(ip)s not in %(net)s network'), params={
                'ip': ip,
                'net': network
            }, code='invalid')

    class Meta:
        db_table = 'ip_pool_employed_ip'
        verbose_name = _('Employed ip')
        verbose_name_plural = _('Employed ip addresses')
        ordering = ('-id',)
        unique_together = ('ip', 'network')


# class LeasesHistory(models.Model):
#     ip = models.GenericIPAddressField(verbose_name=_('Ip address'))
#     lease_time = models.DateTimeField(_('Lease time'), auto_now_add=True)
#     mac_addr = MACAddressField(_('Mac address'), null=True, blank=True)