# coding: utf-8
# vim:sw=4:ts=4:et:
"""Python Ring Doorbell wrapper."""
try:
    from urllib.parse import urlencode
except ImportError:
    from urllib import urlencode

from datetime import timedelta
from datetime import datetime as dt

import logging
import time
import requests

from ring_doorbell.utils import _exists_cache, _save_cache, _read_cache

from ring_doorbell.const import (
    API_VERSION, API_URI, CACHE_ATTRS, CACHE_FILE,
    DEVICES_ENDPOINT, HEADERS, NEW_SESSION_ENDPOINT, MSG_GENERIC_FAIL,
    POST_DATA, PERSIST_TOKEN_ENDPOINT, PERSIST_TOKEN_DATA, RETRY_TOKEN,
    TIMEOUT)

from ring_doorbell.doorbot import RingDoorBell
from ring_doorbell.chime import RingChime
from ring_doorbell.stickup_cam import RingStickUpCam
from ring_doorbell.auth import Auth


_LOGGER = logging.getLogger(__name__)


# pylint: disable=useless-object-inheritance
class Ring(object):
    """A Python Abstraction object to Ring Door Bell."""

    def __init__(self, username, password,
                 auth_callback=None,
                 debug=False, persist_token=False,
                 push_token_notify_url="http://localhost/", reuse_session=True,
                 cache_file=CACHE_FILE, timeout=TIMEOUT):
        """Initialize the Ring object.
        :type auth_callback: Callable[[], str]
        """
        self.is_connected = None
        self.token = None
        self.params = None
        self._persist_token = persist_token
        self._push_token_notify_url = push_token_notify_url
        self._timeout = timeout

        self.debug = debug
        self.username = username
        self.password = password
        self.session = requests.Session()

        self.auth_callback = auth_callback
        self.auth = None
        self.last_refresh = None

        self.cache = CACHE_ATTRS
        self.cache['account'] = self.username
        self.cache_file = cache_file
        self._reuse_session = reuse_session

        # tries to re-use old session
        if self._reuse_session:
            # self.cache['token'] = self.token
            self._process_cached_session()
        else:
            self._authenticate()

    def _process_cached_session(self):
        """Process cache_file to reuse token instead."""
        if _exists_cache(self.cache_file):
            self.cache = _read_cache(self.cache_file)

            # if self.cache['token'] is None, the cache file was corrupted.
            # of if self.cache['account'] does not match with self.username
            # In both cases, a new auth token is required.
            if (self.cache['token'] is None) or \
               (self.cache['account'] is None) or \
               (self.cache['account'] != self.username):
                self._authenticate()
            else:
                # we need to set the self.token and self.params
                # to make use of the self.query() method
                self.token = self.cache['token']
                self.params = {'api_version': API_VERSION,
                               'auth_token': self.token}

                if 'auth' in self.cache:
                    self.auth = self.cache['auth']

                # test if token from cache_file is still valid and functional
                # if not, it should continue to get a new auth token
                url = API_URI + DEVICES_ENDPOINT
                req = self.query(url, raw=True)
                if req and req.status_code == 200:
                    self._authenticate(session=req)
                else:
                    self._authenticate()
        else:
            # first time executing, so we have to create a cache file
            self._authenticate()

    def _get_oauth_token(self):
        """Return Oauth Bearer token."""
        # this token should be cached / saved for later
        oauth = Auth(self.auth)

        if not self.auth:
            self.auth = oauth.fetch_token(
                self.username,
                self.password,
                self.auth_callback)
        else:
            if self.last_refresh:
                expires_in = timedelta(seconds=self.auth['expires_in'])
                refresh_at = self.last_refresh + expires_in

                if self.debug:
                    _LOGGER.debug("response from get oauth token %s",
                                  str(self.auth))

            if not self.last_refresh or (dt.now() >= refresh_at):
                self.auth = oauth.refresh_tokens()
                self.last_refresh = dt.now()
            else:
                if self.debug:
                    _LOGGER.debug("Reusing oauth token %s", str(self.auth))

        return self.auth['access_token']

    def _authenticate(self, attempts=RETRY_TOKEN, session=None, wait=1.0):
        """Authenticate user against Ring API."""
        url = API_URI + NEW_SESSION_ENDPOINT
        loop = 0
        # make a copy as we're mutating headers in the loop below
        # which would cause issues with _get_oauth_token()
        # which expects a non mutated HEADERS copy
        modified_headers = HEADERS.copy()
        while loop <= attempts:
            modified_headers['Authorization'] = \
                'Bearer {}'.format(self._get_oauth_token())
            loop += 1

            try:
                if session is None:
                    req = self.session.post((url),
                                            data=POST_DATA,
                                            headers=modified_headers,
                                            timeout=self._timeout)
                else:
                    req = session
            except requests.exceptions.RequestException as err_msg:
                _LOGGER.error("Error!! %s", err_msg)
                raise

            if not req:
                time.sleep(wait)  # add a pause or you'll get rate limited
                continue

            # if token is expired, refresh credentials and try again
            if req.status_code == 200 or req.status_code == 201:

                # the only way to get a JSON with token is via POST,
                # so we need a special conditional for 201 code
                if req.status_code == 201:
                    data = req.json().get('profile')
                    self.token = data.get('authentication_token')

                self.is_connected = True
                self.params = {'api_version': API_VERSION,
                               'auth_token': self.token}

                if self._persist_token and self._push_token_notify_url:
                    url = API_URI + PERSIST_TOKEN_ENDPOINT
                    PERSIST_TOKEN_DATA['auth_token'] = self.token
                    PERSIST_TOKEN_DATA['device[push_notification_token]'] = \
                        self._push_token_notify_url
                    req = self.session.put((url), headers=modified_headers,
                                           data=PERSIST_TOKEN_DATA,
                                           timeout=self._timeout)

                # update token if reuse_session is True
                if self._reuse_session:
                    self.cache['account'] = self.username
                    self.cache['token'] = self.token
                    self.cache['auth'] = self.auth
                    _save_cache(self.cache, self.cache_file)

                return True

        self.is_connected = False
        req.raise_for_status()
        return True

    def query(self,
              url,
              attempts=RETRY_TOKEN,
              method='GET',
              raw=False,
              extra_params=None,
              json=None,
              timeout=None):
        """Query data from Ring API."""

        # Configure timeout specific to this query
        if timeout is None:
            query_timeout = self._timeout
        else:
            query_timeout = timeout

        if self.debug:
            _LOGGER.debug("Querying %s", url)

        if self.debug and not self.is_connected:
            _LOGGER.debug("Not connected. Refreshing token...")
            self._authenticate()

        # queries now need a bearer token or you'll get 401s
        auth_header = {}
        auth_header['Authorization'] = \
            'Bearer {}'.format(self._get_oauth_token())

        response = None
        loop = 0
        while loop <= attempts:
            if self.debug:
                _LOGGER.debug("running query loop %s", loop)

            # allow to override params when necessary
            # and update self.params globally for the next connection
            if extra_params:
                params = self.params
                params.update(extra_params)
            else:
                params = self.params

            loop += 1
            try:
                if method == 'GET':
                    req = self.session.get(
                        (url), params=urlencode(params),
                        headers=auth_header, timeout=query_timeout)
                elif method == 'PUT':
                    req = self.session.put(
                        (url), params=urlencode(params),
                        headers=auth_header, timeout=query_timeout)
                elif method == 'POST':
                    req = self.session.post(
                        (url), params=urlencode(params), json=json,
                        headers=auth_header, timeout=query_timeout)

                if self.debug:
                    _LOGGER.debug("_query %s ret %s", loop, req.status_code)

            except requests.exceptions.RequestException as err_msg:
                _LOGGER.error("Error!! %s", err_msg)
                raise

            # if token is expired, refresh credentials and try again
            if req.status_code == 401:
                self.is_connected = False
                self._authenticate()
                continue

            if req.status_code == 200 or req.status_code == 204:
                # if raw, return session object otherwise return JSON
                if raw:
                    response = req
                else:
                    if method == 'GET':
                        response = req.json()
                break

        if self.debug and response is None:
            _LOGGER.debug("%s", MSG_GENERIC_FAIL)
        return response

    @property
    def devices(self):
        """Return all devices."""
        devs = {}
        devs['chimes'] = self.chimes
        devs['stickup_cams'] = self.stickup_cams
        devs['doorbells'] = self.doorbells
        return devs

    def __devices(self, device_type):
        """Private method to query devices."""
        lst = []
        url = API_URI + DEVICES_ENDPOINT
        try:
            if device_type == 'stickup_cams':
                req = self.query(url).get('stickup_cams')
                for member in list((obj['description'] for obj in req)):
                    lst.append(RingStickUpCam(self, member))

            if device_type == 'chimes':
                req = self.query(url).get('chimes')
                for member in list((obj['description'] for obj in req)):
                    lst.append(RingChime(self, member))

            if device_type == 'doorbells':
                req = self.query(url).get('doorbots')
                for member in list((obj['description'] for obj in req)):
                    lst.append(RingDoorBell(self, member))

                # get shared doorbells, however device is read-only
                req = self.query(url).get('authorized_doorbots')
                for member in list((obj['description'] for obj in req)):
                    lst.append(RingDoorBell(self, member, shared=True))

        except AttributeError:
            pass
        return lst

    @property
    def chimes(self):
        """Return a list of RingDoorChime objects."""
        return self.__devices('chimes')

    @property
    def stickup_cams(self):
        """Return a list of RingStickUpCam objects."""
        return self.__devices('stickup_cams')

    @property
    def doorbells(self):
        """Return a list of RingDoorBell objects."""
        return self.__devices('doorbells')

    def update(self):
        """Refreshes attributes for all linked devices."""
        for device_lst in self.devices.values():
            for device in device_lst:
                if hasattr(device, "update"):
                    _LOGGER.debug("Updating attributes from %s", device.name)
                    getattr(device, "update")
        return True
