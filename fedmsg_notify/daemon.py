# This file is a part of fedmsg-notify.
#
# fedmsg-notify is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# fedmsg-notify is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with fedmsg-notify.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright (C) 2012, 2013 Red Hat, Inc.
# Author: Luke Macken <lmacken@redhat.com>

from twisted.internet import gtk3reactor
gtk3reactor.install()
from twisted.internet import reactor
from twisted.web.client import downloadPage
from twisted.internet import defer
from twisted.internet.error import ReactorNotRunning

import os
import sys
import json
import uuid
import atexit
import shutil
import psutil
import hashlib
import logging
import dbus
import dbus.glib
import dbus.service
import tempfile
import moksha.hub
import fedmsg.text
import fedmsg.consumers
import fmn.lib
import requests

from gi.repository import Notify, Gio, GLib

from filters import get_enabled_filters, filters as all_filters

log = logging.getLogger('moksha.hub')
pidfile = os.path.expanduser('~/.fedmsg-notify.pid')


class FedmsgNotifyService(dbus.service.Object, fedmsg.consumers.FedmsgConsumer):
    """The Fedmsg Notification Daemon.

    This service is started through DBus activation by calling the
    :meth:`Enable` method, and stopped with :meth:`Disable`.

    This class is not only a DBus service, it is also a Moksha message consumer
    that listens to all messages coming from the Fedora Infrastructure. Moksha
    handles automatically connecting to the remote message hub, subscribing to
    all topics, and calling our :meth:`consume` method with each decoded
    message.

    """
    config_key = 'fedmsg.consumers.notifyconsumer.enabled'
    bus_name = 'org.fedoraproject.fedmsg.notify'
    _object_path = '/org/fedoraproject/fedmsg/notify'
    msg_received_signal = 'org.fedoraproject.fedmsg.notify.MessageReceived'
    service_filters = []  # A list of regex filters from the fedmsg text processors
    enabled = False
    emit_dbus_signals = None  # Allow us to proxy fedmsg to dbus
    enabled_filters = []
    filters = []
    notifications = []

    _icon_cache = {}
    __name__ = "FedmsgNotifyService"

    def __call__(self, hub):
        """ This is a silly hack to help us bridge the gap between
        moksha-land and dbus-land.
        """
        return self

    def __init__(self):
        moksha.hub.setup_logger(verbose='-v' in sys.argv)
        self.settings = Gio.Settings.new(self.bus_name)
        self.emit_dbus_signals = self.settings.get_boolean('emit-dbus-signals')
        self.max_notifications = self.settings.get_int('max-notifications')
        self.topic = self.settings.get_string('topic')
        self.expire = self.settings.get_int('expiration')

        self.fmn_url = self.settings.get_string('fmn-url')
        self.use_server_prefs = self.settings.get_boolean('use-server-prefs')
        self._fmn_openid = self.settings.get_string('fmn-openid')
        self._preferences = []
        self._valid_paths = []

        if not self.settings.get_boolean('enabled'):
            log.info('Disabled via %r configuration, exiting...' %
                     self.config_key)
            return

        try:
            self.session_bus = dbus.SessionBus()
        except dbus.exceptions.DBusException:
            log.exception('Unable to connect to DBus SessionBus')
            return
        if self.session_bus.name_has_owner(self.bus_name):
            log.info('Daemon already running. Exiting...')
            return

        self.connect_signal_handlers()

        self.cfg = fedmsg.config.load_config(None, [])
        moksha_options = {
            self.config_key: True,
            "zmq_subscribe_endpoints": ','.join(
                ','.join(bunch) for bunch in
                self.cfg['endpoints'].values()
            ),
        }
        self.cfg.update(moksha_options)
        self.cache_dir = tempfile.mkdtemp()

        fedmsg.text.make_processors(**self.cfg)
        self.settings_changed(self.settings, 'enabled-filters')

        # Despite what fedmsg.config might say about what consumers are enabled
        # and which are not, we're only going to let the central moksha hub know
        # about *our* consumer.  By specifying this here, it won't even check
        # the entry-points list.
        consumers, prods = [self], []
        moksha.hub._hub = moksha.hub.CentralMokshaHub(self.cfg, consumers,
                                                      prods)

        fedmsg.consumers.FedmsgConsumer.__init__(self, moksha.hub._hub)

        bus_name = dbus.service.BusName(self.bus_name, bus=self.session_bus)
        dbus.service.Object.__init__(self, bus_name, self._object_path)

        Notify.init("fedmsg")
        note = Notify.Notification.new("fedmsg", "activated", "fedmsg-notify")
        note.show()
        reactor.callLater(3.0, note.close)
        self.notifications.insert(0, note)
        self.enabled = True

    def connect_signal_handlers(self):
        self.setting_conn = self.settings.connect(
            'changed::enabled-filters', self.settings_changed)
        self.settings.connect('changed::emit-dbus-signals',
                              self.settings_changed)
        self.settings.connect('changed::filter-settings',
                              self.settings_changed)
        self.settings.connect('changed::expiration', self.settings_changed)

    def settings_changed(self, settings, key):
        self.enabled_filters = get_enabled_filters(self.settings)
        if key == 'enabled-filters':
            log.debug('Reloading filter settings')
            self.service_filters = [processor.__prefix__
                                    for processor in fedmsg.text.processors
                                    if processor.__name__ in self.enabled_filters]
            filter_settings = json.loads(self.settings.get_string('filter-settings'))
            enabled = [filter.__class__.__name__ for filter in self.filters]
            for filter in all_filters:
                name = filter.__name__
                # Remove any filters that were just disabled
                if name in enabled and name not in self.enabled_filters:
                    log.debug('Removing filter: %s' % name)
                    for loaded_filter in [f for f in self.filters if
                                          f.__class__.__name__ == name]:
                        self.filters.remove(loaded_filter)
                # Initialize any filters that were just enabled
                if name not in enabled and name in self.enabled_filters:
                    log.debug('Initializing filter: %s' % name)
                    self.filters.append(filter(filter_settings.get(name, '')))
        elif key == 'filter-settings':
            # We don't want to re-initialize all of our filters here, because
            # this could happen for every keystroke the user types in a text
            # entry. Instead, we do the initialization whenever the list of
            # enabled filters changes.
            pass
        elif key == 'emit-dbus-signals':
            self.emit_dbus_signals = settings.get_boolean(key)
        elif key == 'expiration':
            self.expire = self.settings.get_int('expiration')
        else:
            log.warn('Unknown setting changed: %s' % key)

    @property
    def username(self):
        import fedora_cert
        return fedora_cert.read_user_cert()

    @property
    def openid(self):
        if not self._fmn_openid:
            self._fmn_openid = "{user}.id.fedoraproject.org".format(
                user=self.username)
        return self._fmn_openid

    @property
    def preferences(self):
        def repopulate_functions(preference):
            for fltr in preference['filters']:
                for rule in fltr['rules']:
                    code_path = str(rule['code_path'])
                    rule['fn'] = fedmsg.utils.load_class(code_path)

            return preference


        if not self._preferences:
            url = self.fmn_url + self.openid + "/desktop/"
            log.info("Getting preferences from %s" % url)

            response = requests.get(url)
            if not response:
                log.warning("Failed with %r" % response)
                return []

            preference = response.json()
            preference = repopulate_functions(preference)
            self._preferences = [preference]
        return self._preferences

    @property
    def valid_paths(self):
        if not self._valid_paths:
            self._valid_paths = fmn.lib.load_rules(root="fmn.rules")
        return self._valid_paths

    def consume(self, msg):
        """ Called by fedmsg (Moksha) with each message as they arrive """
        msg, topic = msg.get('body'), msg.get('topic')

        # Here we have two totally different methods for determining what
        # messages to show.  One way allows using preferences as queried from a
        # web service, namely https://apps.fedoraproject.org/notifications
        # The other allows using preferences from a local set kept in gsettings
        if self.use_server_prefs:
            if '.fmn.' in topic:
                openid = msg['msg']['openid']
                if openid == self.openid:
                    log.info("Noticed a pref change for %s", openid)
                    self._preferences = None  # This will trigger a reload.

            recipients = fmn.lib.recipients(
                self.preferences, msg, self.valid_paths, self.cfg)

            if not recipients:
                log.debug("Message to %s didn't match filters" % topic)
                return
        else:
            processor = fedmsg.text.msg2processor(msg)
            for filter in self.filters:
                if filter.match(msg, processor):
                    log.debug('Matched topic %s with %s' % (topic, filter))
                    break
            else:
                for filter in self.service_filters:
                    if filter.match(topic):
                        log.debug('Matched topic %s with %s' % (topic, filter.pattern))
                        break
                else:
                    log.debug("Message to %s didn't match filters" % topic)
                    return


        if self.emit_dbus_signals:
            self.MessageReceived(topic, json.dumps(msg))

        self.notify(msg)

    @dbus.service.signal(dbus_interface=bus_name, signature='ss')
    def MessageReceived(self, topic, body):
        pass

    def notify(self, msg):
        d = self.fetch_icons(msg)
        d.addCallbacks(self.display_notification, errback=log.error,
                       callbackArgs=(msg['body'],))

    def display_notification(self, results, body, *args, **kw):
        pretty_text = fedmsg.text.msg2repr(body, **self.cfg)
        log.debug(pretty_text)
        title, subtitle = self.format_text(body)
        icon, secondary_icon = self.get_icons(body)
        note = Notify.Notification.new(title, subtitle, icon)
        if secondary_icon:
            note.set_hint_string('image-path', secondary_icon)
        try:
            note.show()
            self.notifications.insert(0, note)
            if len(self.notifications) >= self.max_notifications:
                self.notifications.pop().close()
            if self.expire:
                reactor.callLater(self.expire, note.close)
        except:
            log.exception('Unable to display notification')

    def format_text(self, body):
        title = fedmsg.text.msg2title(body, **self.cfg) or ''
        subtitle = fedmsg.text.msg2subtitle(body, **self.cfg) or ''
        link = fedmsg.text.msg2link(body, **self.cfg) or ''
        if link:
            subtitle = u'{} {}'.format(subtitle, link)
        return title, subtitle

    def get_icons(self, body):
        icon = self._icon_cache.get(fedmsg.text.msg2icon(body, **self.cfg))
        secondary_icon = self._icon_cache.get(
            fedmsg.text.msg2secondary_icon(body, **self.cfg))
        ico = hint = None
        if secondary_icon:
            ico = secondary_icon
            hint = icon and icon or secondary_icon
        elif icon:
            ico = hint = icon
        return ico, hint

    def fetch_icons(self, msg):
        icons = []
        body = msg.get('body')
        icon = fedmsg.text.msg2icon(body, **self.cfg)
        if icon:
            icons.append(self.get_icon(icon))
        secondary_icon = fedmsg.text.msg2secondary_icon(body, **self.cfg)
        if secondary_icon:
            icons.append(self.get_icon(secondary_icon))
        return defer.DeferredList(icons)

    def get_icon(self, icon):
        icon_file = self._icon_cache.get(icon)
        if not icon_file:
            icon_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(icon)))
            filename = os.path.join(self.cache_dir, icon_id)
            if not os.path.exists(filename):
                log.debug('Downloading icon: %s' % icon)
                d = downloadPage(str(icon), filename)
                d.addCallbacks(self.cache_icon, errback=log.error,
                               callbackArgs=(icon, filename))
                return d
            else:
                self._icon_cache[icon] = filename
        d = defer.Deferred()
        d.callback(None)
        return d

    def cache_icon(self, results, icon_url, filename):
        if not os.path.exists(filename):
            log.debug('Failed to download %s' % icon_url)
            return
        cache = self._icon_cache
        checksum = self.hash_file(filename)
        if checksum in cache:
            cache[icon_url] = cache[checksum]
            os.unlink(filename)
        else:
            cache[icon_url] = cache[checksum] = filename

    def hash_file(self, filename):
        md5 = hashlib.md5(usedforsecurity=False)
        with open(filename) as f:
            md5.update(f.read())
        return md5.hexdigest()

    @dbus.service.method(bus_name)
    def Enable(self, *args, **kw):
        """ A noop method called to activate this service over dbus """

    @dbus.service.method(bus_name)
    def Disable(self, *args, **kw):
        self.__del__()

    def __del__(self):
        if not self.enabled:
            return
        self.enabled = False

        try:
            for note in self.notifications:
                note.close()
        except GLib.GError:  # Bug 1053160
            pass

        Notify.uninit()

        self.hub.close()
        try:
            reactor.stop()
        except ReactorNotRunning:
            pass

        shutil.rmtree(self.cache_dir, ignore_errors=True)
        if os.path.exists(pidfile):
            os.unlink(pidfile)


def main():
    if os.path.exists(pidfile):
        try:
            with file(pidfile) as f:
                proc = psutil.Process(int(f.read()))
                if proc.name != 'fedmsg-notify-d':
                    os.unlink(pidfile)
                else:
                    return
        except psutil.NoSuchProcess:
            os.unlink(pidfile)
        except ValueError:
            pass

    with file(pidfile, 'w') as f:
        try:
            f.write(str(os.getpid()))
        except IOError:
            log.exception('Unable to write pidfile')

    service = FedmsgNotifyService()
    if service.enabled:
        atexit.register(service.__del__)
        reactor.run()

if __name__ == '__main__':
    main()
