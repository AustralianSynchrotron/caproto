import copy
import functools
import itertools
import threading
import time
import types
from collections.abc import Iterable
from math import log10

import caproto as ca
from caproto import (AccessRights, CaprotoNotImplementedError,
                     CaprotoRuntimeError, CaprotoTimeoutError,
                     CaprotoValueError, ChannelType, SubscriptionType,
                     field_types)

from ..client.common import AUTOMONITOR_MAXLENGTH, STR_ENC
from .client import Context, SharedBroadcaster

__all__ = ('PV', 'get_pv', 'caget', 'caput')


@functools.lru_cache(1)
def _make_context():
    return Context(SharedBroadcaster())


class AccessRightsException(ca.CaprotoError):
    ...


def ensure_connection(func):
    # TODO get timeout default from func signature
    @functools.wraps(func)
    def inner(self, *args, **kwargs):
        self.wait_for_connection(timeout=kwargs.get('timeout', 5.0))
        return func(self, *args, **kwargs)
    return inner


def _parse_dbr_metadata(dbr_data):
    'DBR data -> pyepics metadata dict'
    ret = {}

    arg_map = {'status': 'status',
               'severity': 'severity',
               'precision': 'precision',
               'units': 'units',
               'upper_disp_limit': 'upper_disp_limit',
               'lower_disp_limit': 'lower_disp_limit',
               'upper_alarm_limit': 'upper_alarm_limit',
               'upper_warning_limit': 'upper_warning_limit',
               'lower_warning_limit': 'lower_warning_limit',
               'lower_alarm_limit': 'lower_alarm_limit',
               'upper_ctrl_limit': 'upper_ctrl_limit',
               'lower_ctrl_limit': 'lower_ctrl_limit',
               'enum_strings': 'enum_strs',
               # 'secondsSinceEpoch': 'posixseconds',
               # 'nanoSeconds': 'nanoseconds',
               }

    for attr, arg in arg_map.items():
        if hasattr(dbr_data, attr):
            ret[arg] = getattr(dbr_data, attr)

    if ret.get('enum_strs', None):
        ret['enum_strs'] = tuple(
            k.decode(STR_ENC) for k in ret['enum_strs']
        )

    if hasattr(dbr_data, 'nanoSeconds'):
        ret['posixseconds'] = dbr_data.secondsSinceEpoch
        ret['nanoseconds'] = dbr_data.nanoSeconds
        timestamp = ca.epics_timestamp_to_unix(dbr_data.secondsSinceEpoch,
                                               dbr_data.nanoSeconds)
        ret['timestamp'] = timestamp

    if 'units' in ret:
        ret['units'] = ret['units'].decode(STR_ENC)

    return ret


def _read_response_to_pyepics(full_type, command, enum_strings=None):
    'Parse a ReadResponse command into a pyepics-friendly dict'
    info = _parse_dbr_metadata(command.metadata)

    value = command.data
    info['raw_value'] = value
    info['value'] = _scalarify(value, command.data_type, command.data_count)

    if full_type in ca.char_types:
        value = value.tobytes().partition(b'\x00')[0].decode(STR_ENC)
        info['char_value'] = value
    elif full_type in ca.string_types:
        value = [v.decode(STR_ENC) for v in value]
        if len(value) == 1:
            value = value[0]
        info['value'] = value
        info['char_value'] = value
    elif full_type in ca.enum_types:
        enum_strings = info.get('enum_strs', None) or enum_strings
        if enum_strings is None:
            # This None marker will allow get_ctrlvars to be automatically
            # called later through `_getarg()` magic.
            char_value = [None] * len(value)
        else:
            char_value = [
                enum_strings[idx] if 0 <= idx < len(enum_strings) else ''
                for idx in value
            ]
        if len(char_value) == 1:
            char_value = char_value[0]
        info['char_value'] = char_value
    else:
        info['char_value'] = None

    return info


def _scalarify(data, ntype, count):
    if count == 1 and ntype not in (ChannelType.CHAR, ChannelType.STRING):
        return data[0]
    return data


def _pyepics_get_value(value, string_value, full_type, native_count, *,
                       requested_count, enum_strings, as_string, as_numpy):
    'Handle all the fun pyepics get() kwargs'
    if (as_string and (full_type in ca.char_types) or
            full_type in ca.string_types):
        return string_value
    if as_string and full_type in ca.enum_types:
        if enum_strings is None:
            raise CaprotoValueError('Enum strings unset')
        ret = []
        for r in value:
            try:
                ret.append(enum_strings[r])
            except IndexError:
                ret.append('')
        if len(ret) == 1:
            ret, = ret
        return ret

    elif native_count == 1 and len(value) == 1:
        if requested_count is None:
            return value.tolist()[0]
        else:
            return value

    elif not as_numpy:
        return value.tolist()

    if not value.dtype.isnative:
        value = value.byteswap().view(value.dtype.newbyteorder('='))

    return value


DEFAULT_SUBSCRIPTION_MASK = (SubscriptionType.DBE_VALUE |
                             SubscriptionType.DBE_ALARM)


class PV:
    """Epics Process Variable

    A PV encapsulates an Epics Process Variable.

    The primary interface methods for a pv are to get() and put() is value::

      >>> p = PV(pv_name)  # create a pv object given a pv name
      >>> p.get()          # get pv value
      >>> p.put(val)       # set pv to specified value.

    Additional important attributes include::

      >>> p.pvname         # name of pv
      >>> p.value          # pv value (can be set or get)
      >>> p.char_value     # string representation of pv value
      >>> p.count          # number of elements in array pvs
      >>> p.type           # EPICS data type:'string','double','enum','long',..
"""

    _fmtsca = ("<PV '{pvname}', count={count}, type={typefull!r}, "
               "access={access}>")
    _fmtarr = ("<PV '{pvname}', count={count}/{nelm}, type={typefull!r}, "
               "access={access}>")
    _fields = ('pvname', 'value', 'char_value', 'status', 'ftype', 'chid',
               'host', 'count', 'access', 'write_access', 'read_access',
               'severity', 'timestamp', 'posixseconds', 'nanoseconds',
               'precision', 'units', 'enum_strs',
               'upper_disp_limit', 'lower_disp_limit', 'upper_alarm_limit',
               'lower_alarm_limit', 'lower_warning_limit',
               'upper_warning_limit', 'upper_ctrl_limit', 'lower_ctrl_limit',
               'put_complete')

    @functools.cached_property
    def _default_context(self):
        return _make_context()

    def __init__(self, pvname, callback=None, form='time',
                 verbose=False, auto_monitor=None, count=None,
                 connection_callback=None,
                 connection_timeout=None, access_callback=None, *,
                 context=None):

        if context is None:
            context = self._default_context
        if context is None:
            raise CaprotoRuntimeError("must have a valid context")
        self._context = context
        self.pvname = pvname.strip()
        self.form = form.lower()
        self.verbose = verbose
        self.auto_monitor = auto_monitor
        self.ftype = None
        self._connected = False
        self._connect_event = threading.Event()
        self._state_lock = threading.RLock()
        self.connection_timeout = connection_timeout
        self.default_count = count
        self._auto_monitor_sub = None

        if self.connection_timeout is None:
            self.connection_timeout = 1

        self._args = {}.fromkeys(self._fields)
        self._args['pvname'] = self.pvname
        self._args['count'] = count
        self._args['nelm'] = -1
        self._args['type'] = None
        self._args['typefull'] = None
        self._args['access'] = None
        self.connection_callbacks = []
        self._cb_count = iter(itertools.count())

        if connection_callback is not None:
            self.connection_callbacks = [connection_callback]

        self.access_callbacks = []
        if access_callback is not None:
            self.access_callbacks.append(access_callback)

        self.callbacks = {}
        self._conn_started = False

        if isinstance(callback, (tuple, list)):
            for i, thiscb in enumerate(callback):
                if callable(thiscb):
                    self.callbacks[i] = (thiscb, {})

        elif callable(callback):
            self.callbacks[0] = (callback, {})

        self._caproto_pv, = self._context.get_pvs(
            self.pvname,
            connection_state_callback=self._connection_state_changed,
            access_rights_callback=self._access_rights_changed,
        )

        if self._caproto_pv.connected:
            # connection state callback was already called but we didn't see it
            self._connection_state_changed(self._caproto_pv, 'connected')

    @property
    def connected(self):
        'Connection state'
        return self._caproto_pv.connected and self._connect_event.is_set()

    def force_connect(self, pvname=None, chid=None, conn=True, **kws):
        # not quite sure what this is for in pyepics
        raise CaprotoNotImplementedError

    def wait_for_connection(self, timeout=None):
        """wait for a connection that started with connect() to finish
        Returns
        -------
        connected : bool
            If the PV is connected when this method returns
        """
        if timeout is None:
            timeout = self.connection_timeout

        with self._state_lock:
            if self.connected:
                return True

            self._connect_event.clear()

        self._caproto_pv.wait_for_connection(timeout=timeout)

        # TODO shorten timeouts based on the above
        ok = self._connect_event.wait(timeout=timeout)
        ok = ok and self.connected

        if not ok:
            raise CaprotoTimeoutError(f'{self.pvname} failed to connect within '
                                      f'{timeout} seconds '
                                      f'(caproto={self._caproto_pv})')

        return True

    def _connection_closed(self):
        'Callback when connection is closed'
        self._connected = False

    def _connection_established(self):
        'Callback when connection is initially established'
        ch = self._caproto_pv.channel
        form = self.form
        count = self.default_count

        if ch is None:
            return

        type_key = 'control' if form == 'ctrl' else form
        self._args.update(
            type=ch.native_data_type,
            typefull=field_types[type_key][ch.native_data_type],
            nelm=ch.native_data_count,
            count=ch.native_data_count,
        )
        self._access_rights_changed(self._caproto_pv, ch.access_rights)

        if self.auto_monitor is None:
            mcount = count if count is not None else ch.native_data_count
            self.auto_monitor = mcount < AUTOMONITOR_MAXLENGTH

        self._check_auto_monitor_sub()
        self._connected = True

    def _check_auto_monitor_sub(self, count=None):
        'Ensure auto-monitor subscription is running'
        if self.auto_monitor and not self._auto_monitor_sub:
            if count is None:
                count = self.default_count

            mask = (DEFAULT_SUBSCRIPTION_MASK if self.auto_monitor is True
                    else self.auto_monitor)
            self._auto_monitor_sub = self._caproto_pv.subscribe(
                data_type=self.typefull, data_count=count,
                mask=mask)
            self._auto_monitor_sub.add_callback(self.__on_changes)

    def _connection_state_changed(self, caproto_pv, state):
        'Connection callback hook from threading.PV.connection_state_changed'
        # Ensure _caproto_pv is set, as this callback may happen prior to that in
        # the initializer.  While not necessary in this function, callbacks
        # chained from here may interact with this instance in ways that
        # require it to be set.  For example:
        #   PV created -> connection_state_changed -> run connection_callbacks
        #   -> pv.get_ctrlvars()
        self._caproto_pv = caproto_pv
        connected = (state == 'connected')
        with self._state_lock:
            try:
                if connected:
                    self._connection_established()
            except Exception:
                raise
            finally:
                if connected:
                    self._connect_event.set()

        # todo move to async connect logic
        for cb in self.connection_callbacks:
            cb(pvname=self.pvname, conn=connected, pv=self)

    def connect(self, timeout=None):
        """check that a PV is connected, forcing a connection if needed

        Returns
        -------
        connected : bool
            If the PV is connected when this method returns
        """
        self.wait_for_connection(timeout=timeout)

    def reconnect(self):
        "try to reconnect PV"

        return True

    @ensure_connection
    def get_with_metadata(self, *, count=None, as_string=False, as_numpy=True,
                          timeout=None, with_ctrlvars=False, use_monitor=True,
                          form=None, as_namespace=False):
        """Returns a dictionary of the current value and associated metadata

        Parameters
        ----------
        count : int, optional
             explicitly limit count for array data
        as_string : bool, optional
            flag(True/False) to get a string representation
            of the value.
        as_numpy : bool, optional
            use numpy array as the return type for array data.
        timeout : float, optional
            maximum time to wait for value to be received.
            (default = 0.5 + log10(count) seconds)
        use_monitor : bool, optional
            use value from latest monitor callback (True, default)
            or to make an explicit CA call for the value.
        form : {'time', 'ctrl', None}
            Optionally change the type of the get request
        as_namespace : bool, optional
            Change the return type to that of a namespace with support for
            tab-completion

        Returns
        -------
        val : dict or namespace
           The dictionary of data, guaranteed to at least have the 'value' key.
           Depending on the request form, other keys may also be present::
               {'precision', 'units', 'status', 'severity', 'enum_strs',
               'status', 'severity', 'timestamp', 'posixseconds',
               'nanoseconds', 'upper_disp_limit', 'lower_disp_limit',
               'upper_alarm_limit', 'upper_warning_limit',
               'lower_warning_limit','lower_alarm_limit', 'upper_ctrl_limit',
               'lower_ctrl_limit'}
           Returns ``None`` if the channel is not connected, `wait=False` was used,
           or the data transfer timed out.
        """
        if form is None:
            form = self.form

        if count is None:
            count = self.default_count

        if timeout is None:
            if count is None:
                timeout = 1.0
            else:
                timeout = 1.0 + log10(max(1, count))

        type_key = 'control' if form == 'ctrl' else form

        if (with_ctrlvars and type_key not in ('control', 'native')):
            md = self.get_with_metadata(
                count=count, as_string=as_string, as_numpy=as_numpy,
                timeout=timeout, with_ctrlvars=False,
                use_monitor=use_monitor, form='control', as_namespace=False)
        elif use_monitor:
            md = self._args.copy()
        else:
            md = {}

        dt = field_types[type_key][self.type]
        if not as_string and dt in ca.char_types:
            re_map = {ChannelType.CHAR: ChannelType.INT,
                      ChannelType.CTRL_CHAR: ChannelType.CTRL_INT,
                      ChannelType.TIME_CHAR: ChannelType.TIME_INT,
                      ChannelType.STS_CHAR: ChannelType.STS_INT}
            dt = re_map[dt]
            # TODO if you want char arrays not as_string
            # force no-monitor rather than
            use_monitor = False

        cached_value = self._args['value']

        # trigger going out to got data from network
        if ((not use_monitor) or
                (self._auto_monitor_sub is None) or
                (cached_value is None) or
                (count is not None and count > len(cached_value))):
            command = self._caproto_pv.read(data_type=dt, data_count=count,
                                            timeout=timeout)
            response = _read_response_to_pyepics(
                self.typefull, command, enum_strings=self._args['enum_strs']
            )
            self._args.update(**response)
            md.update(**response)

        if as_string and self.typefull in ca.enum_types:
            enum_strs = self.enum_strs
        else:
            enum_strs = None

        md['value'] = _pyepics_get_value(
            value=md['raw_value'], string_value=md['char_value'],
            full_type=self.typefull, native_count=self._args['count'],
            requested_count=count, enum_strings=enum_strs, as_string=as_string,
            as_numpy=as_numpy)

        if as_namespace:
            return types.SimpleNamespace(**md)
        return md

    def get(self, *, count=None, as_string=False, as_numpy=True,
            timeout=None, with_ctrlvars=False, use_monitor=True):
        """Returns current value of PV.

        Parameters
        ----------
        count : int, optional
             explicitly limit count for array data
        as_string : bool, optional
            flag(True/False) to get a string representation
            of the value.
        as_numpy : bool, optional
            use numpy array as the return type for array data.
        timeout : float, optional
            maximum time to wait for value to be received.
            (default = 0.5 + log10(count) seconds)
        use_monitor : bool, optional
            use value from latest monitor callback (True, default)
            or to make an explicit CA call for the value.

        Returns
        -------
        val : object
            The value from the PV.
            Returns None in the case of a timeout.
        """
        data = self.get_with_metadata(
            count=count, as_string=as_string, as_numpy=as_numpy,
            timeout=timeout, with_ctrlvars=with_ctrlvars,
            use_monitor=use_monitor, form=self.form, as_namespace=False)

        return (data['value']
                if data is not None
                else None)

    @ensure_connection
    def put(self, value, *, wait=False, timeout=30.0,
            use_complete=False, callback=None, callback_data=None):
        """set value for PV, optionally waiting until the processing is
        complete, and optionally specifying a callback function to be run
        when the processing is complete.
        """
        if callback_data is None:
            callback_data = ()
        if not self._args['write_access']:
            raise AccessRightsException('Cannot put to PV according to write access')

        if self._args['typefull'] in ca.enum_types:
            if isinstance(value, str):
                try:
                    value = self.enum_strs.index(value)
                except ValueError:
                    raise CaprotoValueError('{} is not in Enum ({}'.format(
                        value, self.enum_strs))

        if isinstance(value, str):
            if self.typefull in ca.char_types:
                # have to add a null-terminator char
                value = value.encode(STR_ENC) + b'\0'
            else:
                value = (value, )
        elif not isinstance(value, Iterable):
            value = (value, )

        if len(value) and isinstance(value[0], str):
            value = tuple(v.encode(STR_ENC) for v in value)

        notify = any((use_complete, callback is not None, wait))

        if callback is None and not use_complete:
            run_callback = None
        else:
            def run_callback(cmd):
                if use_complete:
                    self._args['put_complete'] = True
                if callback is not None:
                    callback(*callback_data)

        # So, in pyepics, put_complete strictly refers to the functionality
        # where we toggle 'put_complete' in the args after a
        # WriteNotifyResponse.
        if notify:
            if use_complete:
                self._args['put_complete'] = False
        else:
            wait = False

        self._caproto_pv.write(value, wait=wait, callback=run_callback,
                               timeout=timeout, notify=notify)

    def _read_and_update(self, dtype, timeout):
        """Read the caproto PV with `dtype` and update state _args."""
        command = self._caproto_pv.read(data_type=dtype, timeout=timeout)
        info = _read_response_to_pyepics(
            dtype, command, enum_strings=self._args['enum_strs']
        )
        self._args.update(**info)
        return command, info

    @ensure_connection
    def get_ctrlvars(self, timeout=5, warn=True):
        "get control values for variable"
        _, info = self._read_and_update(
            field_types['control'][self.type], timeout
        )
        self.force_read_access_rights()
        return info

    @ensure_connection
    def get_timevars(self, timeout=5, warn=True):
        "get time values for variable"
        _, info = self._read_and_update(
            field_types['time'][self.type], timeout
        )
        return info

    @ensure_connection
    def force_read_access_rights(self):
        'Force a read of access rights, not relying on last event callback.'
        self._access_rights_changed(self._caproto_pv,
                                    self._caproto_pv.channel.access_rights,
                                    forced=True)

    def _access_rights_changed(self, caproto_pv, access_rights, *,
                               forced=False):
        self._caproto_pv = caproto_pv
        read_access = AccessRights.READ in access_rights
        write_access = AccessRights.WRITE in access_rights
        access_strs = ('no access', 'read-only', 'write-only', 'read/write')
        self._args.update(write_access=write_access,
                          read_access=read_access,
                          access=access_strs[access_rights])

        for cb in self.access_callbacks:
            try:
                cb(read_access, write_access, pv=self)
            except Exception:
                ...

    def __on_changes(self, sub, command):
        """internal callback function: do not overwrite!!
        To have user-defined code run when the PV value changes,
        use add_callback()
        """
        info = _read_response_to_pyepics(
            self.typefull, command, enum_strings=self._args['enum_strs']
        )
        self._args.update(**info)
        self.run_callbacks()

    def run_callbacks(self):
        """run all user-defined callbacks with the current data

        Normally, this is to be run automatically on event, but
        it is provided here as a separate function for testing
        purposes.
        """
        for index in sorted(list(self.callbacks.keys())):
            self.run_callback(index)

    def run_callback(self, index):
        """run a specific user-defined callback, specified by index,
        with the current data
        Note that callback functions are called with keyword/val
        arguments including:
             self._args  (all PV data available, keys = __fields)
             keyword args included in add_callback()
             keyword 'cb_info' = (index, self)
        where the 'cb_info' is provided as a hook so that a callback
        function  that fails may de-register itself (for example, if
        a GUI resource is no longer available).
        """
        try:
            fcn, kwargs = self.callbacks[index]
        except KeyError:
            return
        kwd = copy.copy(self._args)
        kwd.update(kwargs)
        kwd['cb_info'] = (index, self)
        if callable(fcn):
            fcn(**kwd)

    def add_callback(self, callback, *, index=None, run_now=False,
                     with_ctrlvars=True, **kw):
        """add a callback to a PV.  Optional keyword arguments
        set here will be preserved and passed on to the callback
        at runtime.

        Note that a PV may have multiple callbacks, so that each
        has a unique index (small integer) that is returned by
        add_callback.  This index is needed to remove a callback."""
        if not callable(callback):
            raise CaprotoValueError()
        if index is not None:
            raise CaprotoValueError("why do this")
        index = next(self._cb_count)
        self.callbacks[index] = (callback, kw)

        if self.connected:
            self._check_auto_monitor_sub()

        if with_ctrlvars and self.connected:
            self.get_ctrlvars()
        if run_now:
            self.get(as_string=True)
            if self.connected:
                self.run_callback(index)
        return index

    def remove_callback(self, index):
        """remove a callback by index"""
        self.callbacks.pop(index, None)

    def clear_callbacks(self):
        "clear all callbacks"
        self.callbacks = {}

    def __getval(self):
        "get value"
        return self.get()

    def __setval(self, val):
        "put-value"
        return self.put(val)

    value = property(__getval, __setval, None, "value property")

    @property
    def char_value(self):
        "character string representation of value"
        return self._getarg('char_value')

    @property
    def status(self):
        "pv status"
        return self._getarg('status')

    @property
    def type(self):
        "pv type"
        return self._args['type']

    @property
    def typefull(self):
        "pv type"
        return self._args['typefull']

    @property
    def host(self):
        "pv host"
        return self._caproto_pv.circuit_manager.circuit.host

    @property
    def count(self):
        """count (number of elements). For array data and later EPICS versions,
        this is equivalent to the .NORD field.  See also 'nelm' property"""
        if self._args['count'] is not None:
            return self._args['count']
        else:
            return self._getarg('count')

    @property
    def nelm(self):
        """native count (number of elements).
        For array data this will return the full array size (ie, the
        .NELM field).  See also 'count' property"""
        if self._getarg('count') == 1:
            return 1
        return self._caproto_pv.channel.native_data_count

    @property
    def read_access(self):
        "read access"
        return self._getarg('read_access')

    @property
    def write_access(self):
        "write access"
        return self._getarg('write_access')

    @property
    def access(self):
        "read/write access as string"
        return self._getarg('access')

    @property
    def severity(self):
        "pv severity"
        return self._getarg('severity')

    @property
    def timestamp(self):
        "timestamp of last pv action"
        return self._getarg('timestamp')

    @property
    def posixseconds(self):
        """integer seconds for timestamp of last pv action
        using POSIX time convention"""
        return self._getarg('posixseconds')

    @property
    def nanoseconds(self):
        "integer nanoseconds for timestamp of last pv action"
        return self._getarg('nanoseconds')

    @property
    def precision(self):
        "number of digits after decimal point"
        return self._getarg('precision')

    @property
    def units(self):
        "engineering units for pv"
        return self._getarg('units')

    @property
    def enum_strs(self):
        "list of enumeration strings"
        return self._getarg('enum_strs')

    @property
    def upper_disp_limit(self):
        "limit"
        return self._getarg('upper_disp_limit')

    @property
    def lower_disp_limit(self):
        "limit"
        return self._getarg('lower_disp_limit')

    @property
    def upper_alarm_limit(self):
        "limit"
        return self._getarg('upper_alarm_limit')

    @property
    def lower_alarm_limit(self):
        "limit"
        return self._getarg('lower_alarm_limit')

    @property
    def lower_warning_limit(self):
        "limit"
        return self._getarg('lower_warning_limit')

    @property
    def upper_warning_limit(self):
        "limit"
        return self._getarg('upper_warning_limit')

    @property
    def upper_ctrl_limit(self):
        "limit"
        return self._getarg('upper_ctrl_limit')

    @property
    def lower_ctrl_limit(self):
        "limit"
        return self._getarg('lower_ctrl_limit')

    @property
    def info(self):
        "info string"
        return self._getinfo()

    @property
    def put_complete(self):
        "returns True if a put-with-wait has completed"
        return self._args['put_complete']

    def _getinfo(self):
        "get information paragraph"
        self.get_ctrlvars()
        out = []
        xtype = self._args['typefull']
        nt_type = ca.native_type(xtype)
        fmt = '%i'

        if nt_type in (ChannelType.FLOAT, ChannelType.DOUBLE):
            fmt = '%g'
        elif nt_type in (ChannelType.CHAR, ChannelType.STRING):
            fmt = '%s'

        # self._set_charval(self._args['value'], call_ca=False)
        out.append(f"== {self.pvname}  ({ca.DBR_TYPES[xtype].__name__}) ==")
        if self.count == 1:
            val = self._args['value']
            out.append(f'   value      = {fmt}' % val)
        else:
            ext = {True: '...', False: ''}[self.count > 10]
            elems = range(min(5, self.count))
            try:
                aval = [fmt % self._args['value'][i] for i in elems]
            except TypeError:
                aval = ('unknown',)
            out.append("   value      = array  [%s%s]" % (",".join(aval), ext))
        for nam in ('char_value', 'count', 'nelm', 'type', 'units',
                    'precision', 'host', 'access',
                    'status', 'severity', 'timestamp',
                    'posixseconds', 'nanoseconds',
                    'upper_ctrl_limit', 'lower_ctrl_limit',
                    'upper_disp_limit', 'lower_disp_limit',
                    'upper_alarm_limit', 'lower_alarm_limit',
                    'upper_warning_limit', 'lower_warning_limit'):
            if hasattr(self, nam):
                att = getattr(self, nam)
                if att is not None:
                    if nam == 'timestamp':
                        def fmt_time(tstamp=None):
                            "simple formatter for time values"
                            if tstamp is None:
                                tstamp = time.time()
                            tstamp, frac = divmod(tstamp, 1)
                            return "%s.%5.5i" % (
                                time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(tstamp)),
                                round(1.e5 * frac))

                        att = "%.3f (%s)" % (att, fmt_time(att))
                    elif nam == 'char_value':
                        att = "'%s'" % att
                    if len(nam) < 12:
                        out.append('   %.11s= %s' % (nam + ' ' * 12, str(att)))
                    else:
                        out.append('   %.20s= %s' % (nam + ' ' * 20, str(att)))
        if xtype == 'enum':  # list enum strings
            out.append('   enum strings: ')
            for index, nam in enumerate(self.enum_strs):
                out.append("       %i = %s " % (index, nam))

        if self._auto_monitor_sub is not None:
            msg = 'PV is internally monitored'
            out.append('   %s, with %i user-defined callbacks:' %
                       (msg, len(self.callbacks)))
            if len(self.callbacks) > 0:
                for nam in sorted(self.callbacks.keys()):
                    cback = self.callbacks[nam][0]
                    out.append('      {!r}'.format(cback))
        else:
            out.append('   PV is NOT internally monitored')
        out.append('=============================')
        return '\n'.join(out)

    def _getarg(self, arg):
        "wrapper for property retrieval"
        if self._args['value'] is None:
            self.get()
        if self._args[arg] is None and self.connected:
            if arg in ('status', 'severity', 'timestamp',
                       'posixseconds', 'nanoseconds'):
                self.get_timevars(warn=False)
            else:
                self.get_ctrlvars(warn=False)
        return self._args.get(arg, None)

    def __repr__(self):
        "string representation"

        if self.connected:
            if self._args['count'] == 1:  # self.count == 1:
                return self._fmtsca.format(**self._args)
            else:
                return self._fmtarr.format(**self._args)
        else:
            return "<PV '%s': not connected>" % self.pvname

    def __eq__(self, other):
        "test for equality"
        return False

    def disconnect(self):
        "disconnect PV"
        if self.connected:
            self._caproto_pv.go_idle()


def get_pv(pvname, *args, context=None, connect=False, timeout=5, **kwargs):
    pv = PV(pvname, *args, context=context, **kwargs)
    if connect:
        pv.wait_for_connection(timeout=timeout)
    return pv


def caput(pvname, value, wait=False, timeout=60, context=None):
    """caput(pvname, value, wait=False, timeout=60)
    simple put to a pv's value.
       >>> caput('xx.VAL',3.0)

    to wait for pv to complete processing, use 'wait=True':
       >>> caput('xx.VAL',3.0,wait=True)
    """
    thispv = get_pv(pvname, connect=True, context=context)
    if thispv.connected:
        return thispv.put(value, wait=wait, timeout=timeout)


def caget(pvname, as_string=False, count=None, as_numpy=True,
          use_monitor=False, timeout=5.0, context=None):
    """caget(pvname, as_string=False)
    simple get of a pv's value..
       >>> x = caget('xx.VAL')

    to get the character string representation (formatted double,
    enum string, etc):
       >>> x = caget('xx.VAL', as_string=True)

    to get a truncated amount of data from an array, you can specify
    the count with
       >>> x = caget('MyArray.VAL', count=1000)
    """
    start_time = time.time()
    thispv = get_pv(pvname, timeout=timeout, connect=True, context=context)
    if thispv.connected:
        if as_string:
            thispv.get_ctrlvars()
        timeout -= (time.time() - start_time)
        val = thispv.get(count=count, timeout=timeout,
                         use_monitor=use_monitor,
                         as_string=as_string,
                         as_numpy=as_numpy)
        return val


def cainfo(pvname, print_out=True):
    """cainfo(pvname,print_out=True)

    return printable information about pv
       >>>cainfo('xx.VAL')

    will return a status report for the pv.

    If print_out=False, the status report will be printed,
    and not returned.
    """
    thispv = get_pv(pvname, connect=True)
    if thispv.connected:
        thispv.get()
        thispv.get_ctrlvars()
        if print_out:
            print(pvname, thispv.info)
        else:
            return thispv.info


def caget_many(pvlist, as_string=False, count=None, as_numpy=True, timeout=5.0,
               context=None, raises=False):
    """get values for a list of PVs

    This does not maintain PV objects, and works as fast
    as possible to fetch many values.
    """
    if context is None:
        context = _make_context()

    pvs = context.get_pvs(*pvlist)

    readings = {}
    pending_pvs = list(pvs)
    t = time.monotonic()
    while pending_pvs and time.monotonic() - t < timeout:
        for pv in list(pending_pvs):
            if pv.connected:
                readings[pv] = pv.read(data_type='control')
                pending_pvs.remove(pv)
        time.sleep(0.01)

    if raises and pending_pvs:
        raise CaprotoTimeoutError(f'{pending_pvs[0].name} failed to connect within '
                                  f'{timeout} seconds '
                                  f'(caproto={pending_pvs[0]})')

    get_kw = dict(as_string=as_string,
                  as_numpy=as_numpy,
                  requested_count=count,
                  )

    def final_get(pv):
        if pv in pending_pvs:
            return None

        # Use "DBR_CTRL_*" so that we can get enum strings, if necessary.
        full_type = field_types['control'][pv.channel.native_data_type]
        enum_strings = getattr(readings[pv].metadata, "enum_strings", None)
        if enum_strings:
            enum_strings = [
                enum_str.decode(STR_ENC) for enum_str in enum_strings
            ]
        info = _read_response_to_pyepics(
            full_type=full_type,
            command=readings[pv],
            enum_strings=enum_strings,
        )
        return _pyepics_get_value(value=info['raw_value'],
                                  string_value=info['char_value'],
                                  full_type=pv.channel.native_data_type,
                                  native_count=pv.channel.native_data_count,
                                  enum_strings=enum_strings,
                                  **get_kw)
    return [final_get(pv) for pv in pvs]


def caput_many(pvlist, values, wait=False, connection_timeout=None,
               put_timeout=60):
    """put values to a list of PVs, as fast as possible

    This does not maintain the PV objects it makes.

    If wait is 'each', *each* put operation will block until
    it is complete or until the put_timeout duration expires.

    If wait is 'all', this method will block until *all*
    put operations are complete, or until the put_timeout
    duration expires.

    Note that the behavior of 'wait' only applies to the
    put timeout, not the connection timeout.

    Returns a list of integers for each PV, 1 if the put
    was successful, or a negative number if the timeout
    was exceeded.
    """
    if len(pvlist) != len(values):
        raise CaprotoValueError("List of PV names must be equal to list of values.")

    # context = PV._default_context
    # TODO: context.get_pvs(...)

    out = []
    pvs = [PV(name, auto_monitor=False, connection_timeout=connection_timeout)
           for name in pvlist]

    wait_all = (wait == 'all')
    wait_each = (wait == 'each')
    for p, v in zip(pvs, values):
        try:
            p.wait_for_connection(connection_timeout)
            p.put(v, wait=wait_each, timeout=put_timeout,
                  use_complete=wait_all)
        except TimeoutError:
            out.append(-1)
        else:
            out.append(1)

    if not wait_all:
        return [o if o == 1 else -1 for o in out]

    start_time = time.time()
    while not all([(p.connected and p.put_complete) for p in pvs]):
        elapsed_time = time.time() - start_time
        if elapsed_time > put_timeout:
            break
    return [1 if (p.connected and p.put_complete) else -1
            for p in pvs]
