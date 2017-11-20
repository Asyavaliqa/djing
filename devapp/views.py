# -*- coding: utf-8 -*-
from django.contrib.auth.decorators import login_required
from django.contrib.gis.shortcuts import render_to_text
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404, resolve_url
from django.contrib import messages
from django.utils.translation import ugettext_lazy as _
from easysnmp import EasySNMPTimeoutError, EasySNMPError
from json import dumps

from .models import Device, Port, DeviceDBException, DeviceMonitoringException
from mydefs import pag_mn, res_success, res_error, only_admins, ping, order_helper
from .forms import DeviceForm, PortForm
from abonapp.models import AbonGroup, Abon
from django.conf import settings
from guardian.decorators import permission_required_or_403 as permission_required
from guardian.shortcuts import get_objects_for_user


@login_required
@only_admins
def devices(request, grp):
    group = get_object_or_404(AbonGroup, pk=grp)
    if not request.user.has_perm('abonapp.can_view_abongroup', group):
        raise PermissionDenied
    try:
        devs = Device.objects.filter(user_group=group).select_related('user_group').only('comment', 'mac_addr', 'devtype', 'user_group', 'pk', 'ip_address')

        # фильтр
        dr, field = order_helper(request)
        if field:
            devs = devs.order_by(field)

        devs = pag_mn(request, devs)
        devs = Device.objects.wrap_monitoring_info(devs)

    except (DeviceDBException, DeviceMonitoringException) as e:
        messages.error(request, e)

    return render(request, 'devapp/devices.html', {
        'devices': devs,
        'dir': dr,
        'order_by': request.GET.get('order_by'),
        'group': group
    })


@login_required
@only_admins
def devices_null_group(request):
    devs = Device.objects.filter(user_group=None).only('comment', 'devtype', 'user_group', 'pk', 'ip_address')
    # фильтр
    dr, field = order_helper(request)
    if field:
        devs = devs.order_by(field)

    devs = pag_mn(request, devs)

    return render(request, 'devapp/devices_null_group.html', {
        'devices': devs,
        'dir': dr,
        'order_by': request.GET.get('order_by')
    })


@login_required
@permission_required('devapp.delete_device')
def devdel(request, did):
    try:
        dev = Device.objects.get(pk=did)
        back_url = resolve_url('devapp:devs', grp=dev.user_group.pk if dev.user_group else 0)
        dev.delete()
        return res_success(request, back_url)
    except Device.DoesNotExist:
        return res_error(request, _('Delete failed'))
    except DeviceDBException as e:
        return res_error(request, e)


@login_required
@permission_required('devapp.can_view_device')
def dev(request, grp, devid=0):
    user_group = get_object_or_404(AbonGroup, pk=grp)
    if not request.user.has_perm('abonapp.can_view_abongroup', user_group):
        raise PermissionDenied
    devinst = get_object_or_404(Device, id=devid) if devid != 0 else None
    already_dev = None

    if request.method == 'POST':
        if devid == 0:
            if not request.user.has_perm('devapp.add_device'):
                raise PermissionDenied
        else:
            if not request.user.has_perm('devapp.change_device'):
                raise PermissionDenied
        frm = DeviceForm(request.POST, instance=devinst)
        if frm.is_valid():
            ndev = frm.save()
            ndev.update_dhcp()
            messages.success(request, _('Device info has been saved'))
            return redirect('devapp:edit', ndev.user_group.pk, ndev.pk)
        else:
            try:
                already_dev = Device.objects.get(mac_addr=request.POST.get('mac_addr'))
                if already_dev.user_group:
                    messages.warning(request, _('You have redirected to existing device'))
                    return redirect('devapp:view', already_dev.user_group.pk, already_dev.pk)
                else:
                    messages.warning(request, _('Please attach user group for device'))
                    return redirect('devapp:fix_device_group', already_dev.pk)
            except Device.DoesNotExist:
                pass
            messages.error(request, _('Form is invalid, check fields and try again'))
    else:
        if devinst is None:
            frm = DeviceForm(initial={
                'user_group': user_group,
                'devtype': request.GET.get('t'),
                'mac_addr': request.GET.get('mac'),
                'comment': request.GET.get('c'),
                'ip_address': request.GET.get('ip'),
                'man_passw': getattr(settings, 'DEFAULT_SNMP_PASSWORD', ''),
                'snmp_item_num': request.GET.get('n')
            })
        else:
            frm = DeviceForm(instance=devinst)

    if devinst is None:
        return render(request, 'devapp/add_dev.html', {
            'form': frm,
            'group': user_group,
            'already_dev': already_dev
        })
    else:
        return render(request, 'devapp/dev.html', {
            'form': frm,
            'dev': devinst,
            'selected_parent_dev': devinst.parent_dev,
            'group': user_group,
            'already_dev': already_dev
        })


@login_required
@permission_required('devapp.change_device')
def manage_ports(request, devid):
    try:
        dev = Device.objects.get(pk=devid)
        if dev.user_group is None:
            messages.error(request, _('Device is not have a group, please fix that'))
            return redirect('devapp:fix_device_group', dev.pk)
        ports = Port.objects.filter(device=dev)

    except Device.DoesNotExist:
        messages.error(request, _('Device does not exist'))
        return redirect('devapp:view', dev.user_group.pk if dev.user_group else 0, did=devid)
    except DeviceDBException as e:
        messages.error(request, e)
    return render(request, 'devapp/manage_ports/list.html', {
        'ports': ports,
        'dev': dev
    })


@login_required
@permission_required('devapp.add_port')
def add_ports(request, devid):
    class TempPort:
        def __init__(self, pid, text, status, from_db, pk=None):
            self.pid = pid
            self.text = text
            self.status = status
            self.from_db = from_db
            self.pk = pk

        def __eq__(self, other):
            return self.pid == other.pid

        def __hash__(self):
            return self.pid

        def __str__(self):
            return "p:%d\tM:%s\tT:%s" % (self.pid, self.text)

    try:
        res_ports = list()
        dev = Device.objects.get(pk=devid)
        if dev.user_group is None:
            messages.error(request, _('Device is not have a group, please fix that'))
            return redirect('devapp:fix_device_group', dev.pk)
        if request.method == 'POST':
            ports = zip(
                request.POST.getlist('p_text'),
                request.POST.getlist('pids')
            )
            for port_text, port_num in ports:
                if port_text == '' or port_text is None:
                    continue
                try:
                    port = Port.objects.get(num=port_num, device=dev)
                    port.descr = port_text
                    port.save(update_fields=['descr'])
                except Port.DoesNotExist:
                    Port.objects.create(
                        num=port_num,
                        device=dev,
                        descr=port_text
                    )

        db_ports = Port.objects.filter(device=dev)
        db_ports = [TempPort(p.num, p.descr, None, True, p.pk) for p in db_ports]

        manager = dev.get_manager_object()
        ports = manager.get_ports()
        if ports is not None:
            ports = [TempPort(p.num, p.nm, p.st, False) for p in ports]
            res_ports = set(db_ports + ports)
        else:
            res_ports = db_ports

    except Device.DoesNotExist:
        messages.error(request, _('Device does not exist'))
        return redirect('devapp:group_list')
    except DeviceDBException as e:
        messages.error(request, e)
    except EasySNMPTimeoutError:
        messages.error(request, _('wait for a reply from the SNMP Timeout'))
    return render(request, 'devapp/manage_ports/add_ports.html', {
        'ports': res_ports,
        'dev': dev
    })


@login_required
@permission_required('devapp.delete_port')
def delete_single_port(request, grp, did, portid):
    try:
        if request.method == 'POST':
            if request.POST.get('confirm') == 'yes':
                Port.objects.get(pk=portid).delete()
                messages.success(request, _('Port successfully removed'))
        else:
            return render_to_text('devapp/manage_ports/modal_del_port.html', {
                'grp': grp,
                'did': did,
                'port_id': portid
            }, request=request)
    except Port.DoesNotExist:
        messages.error(request, _('Port does not exist'))
    except DeviceDBException as e:
        messages.error(request, e)
    return redirect('devapp:manage_ports', grp, did)


@login_required
@permission_required('devapp.add_port')
def edit_single_port(request, grp, did, pid):
    try:
        port = Port.objects.get(pk=pid)
        if request.method == 'POST':
            frm = PortForm(request.POST, instance=port)
            if frm.is_valid():
                frm.save()
                messages.success(request, _('Port successfully saved'))
            else:
                messages.error(request, _('Form is invalid, check fields and try again'))
            return redirect('devapp:manage_ports', grp, did)

        frm = PortForm(instance=port)
        return render_to_text('devapp/manage_ports/modal_add_edit_port.html', {
            'port_id': pid,
            'did': did,
            'gid': grp,
            'form': frm
        }, request=request)
    except Port.DoesNotExist:
        messages.error(request, _('Port does not exist'))
    except DeviceDBException as e:
        messages.error(request, e)
    return redirect('devapp:manage_ports', grp, did)


@login_required
@permission_required('devapp.add_port')
def add_single_port(request, grp, did):
    try:
        device = Device.objects.get(pk=did)
        if request.method == 'POST':
            frm = PortForm(request.POST, instance=Port(device=device))
            if frm.is_valid():
                frm.save()
                messages.success(request, _('Port successfully saved'))
                return redirect('devapp:manage_ports', grp, did)
            else:
                messages.error(request, _('Form is invalid, check fields and try again'))
        else:
            frm = PortForm(initial={
                'num': request.GET.get('n'),
                'descr': request.GET.get('t')
            })
        return render_to_text('devapp/manage_ports/modal_add_edit_port.html', {
            'did': did,
            'gid': grp,
            'form': frm
        }, request=request)
    except Device.DoesNotExist:
        messages.error(request, _('Device does not exist'))
    except DeviceDBException as e:
        messages.error(request, e)
    return redirect('devapp:manage_ports', grp, did)


@login_required
@permission_required('devapp.can_view_device')
def devview(request, did):
    ports, manager = None, None
    dev = get_object_or_404(Device, id=did)

    if not dev.user_group:
        messages.warning(request, _('Please attach user group for device'))
        return redirect('devapp:fix_device_group', dev.pk)

    template_name = 'ports.html'
    try:
        if ping(dev.ip_address):
            if dev.man_passw:
                manager = dev.get_manager_object()
                ports = manager.get_ports()
                template_name = manager.get_template_name()
            else:
                messages.warning(request, _('Not Set snmp device password'))
        else:
            messages.error(request, _('Dot was not pinged'))
        return render(request, 'devapp/custom_dev_page/' + template_name, {
            'dev': dev,
            'ports': ports,
            'dev_accs': Abon.objects.filter(device=dev),
            'dev_manager': manager
        })
    except EasySNMPError:
        messages.error(request, _('SNMP error on device'))
    except DeviceDBException as e:
        messages.error(request, e)
    return render(request, 'devapp/custom_dev_page/' + template_name, {
        'dev': dev
    })


@login_required
@permission_required('devapp.can_toggle_ports')
def toggle_port(request, did, portid, status=0):
    portid = int(portid)
    status = int(status)
    dev = get_object_or_404(Device, id=int(did))
    try:
        if ping(dev.ip_address):
            if dev.man_passw:
                manager = dev.get_manager_klass()(dev.ip_address, dev.man_passw)
                ports = manager.get_ports()
                if status:
                    ports[portid - 1].enable()
                else:
                    ports[portid - 1].disable()
            else:
                messages.warning(request, _('Not Set snmp device password'))
        else:
            messages.error(request, _('Dot was not pinged'))
    except EasySNMPTimeoutError:
        messages.error(request, _('wait for a reply from the SNMP Timeout'))
    except EasySNMPError as e:
        messages.error(request, e)
    return redirect('devapp:view', dev.user_group.pk if dev.user_group is not None else 0, did)


@login_required
@only_admins
def group_list(request):
    groups = AbonGroup.objects.all().order_by('title')
    groups = get_objects_for_user(request.user, 'abonapp.can_view_abongroup', klass=groups, accept_global_perms=False)
    return render(request, 'devapp/group_list.html', {
        'groups': groups
    })


@login_required
def search_dev(request):
    word = request.GET.get('s')
    if word is None:
        results = [{'id': 0, 'text': ''}]
    else:
        results = Device.objects.filter(
            Q(comment__icontains=word) | Q(ip_address=word)
        ).only('pk', 'ip_address', 'comment')[:16]
        results = [{'id': dev.pk, 'text': "%s: %s" % (dev.ip_address, dev.comment)} for dev in results]
    return HttpResponse(dumps(results, ensure_ascii=False))


@login_required
def fix_device_group(request, did):
    dev = get_object_or_404(Device, pk=did)
    try:
        if request.method == 'POST':
            frm = DeviceForm(request.POST, instance=dev)
            if frm.is_valid():
                ch_dev = frm.save()
                if ch_dev.user_group:
                    messages.success(request, _('Device fixed'))
                    return redirect('devapp:devs', ch_dev.user_group.pk)
                else:
                    messages.error(request, _('Please attach user group for device'))
            else:
                messages.error(request, _('Form is invalid, check fields and try again'))
        else:
            frm = DeviceForm(instance=dev)
    except ValueError:
        return HttpResponse('ValueError')
    return render(request, 'devapp/fix_dev_group.html', {
        'form': frm,
        'dev': dev,
        'selected_parent_dev': dev.parent_dev
    })


@login_required
def fix_onu(request):
    mac = request.GET.get('cmd_param')
    status = 1
    text = '<span class="glyphicon glyphicon-exclamation-sign"></span>'
    try:
        onu = Device.objects.get(mac_addr=mac, devtype='On')
        parent = onu.parent_dev
        if parent is not None:
            manobj = parent.get_manager_object()
            ports = manobj.get_list_keyval('.1.3.6.1.4.1.3320.101.10.1.1.3')
            for srcmac, snmpnum in ports:
                real_mac = ':'.join(['%x' % ord(i) for i in srcmac])
                if mac == real_mac:
                    onu.snmp_item_num = snmpnum
                    onu.save(update_fields=['snmp_item_num'])
                    status = 0
                    text = '<span class="glyphicon glyphicon-ok"></span> <span class="hidden-xs">%s</span>' % _('Fixed')
                    break
        else:
            text = text + ' %s' % _('Parent device not found')
    except Device.DoesNotExist:
        pass
    return HttpResponse(dumps({
        'status': status,
        'dat': text
    }))

