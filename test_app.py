"""Tests for app.py."""
import unittest
from unittest.mock import patch

from granary.bluesky import Bluesky
from oauth_dropins.bluesky import BlueskyAuth
from oauth_dropins.webutil.appengine_config import ndb_client
from oauth_dropins.webutil.util import json_dumps
from oauth_dropins.webutil import util
import requests
from requests_oauth2client import (
  DPoPKey,
  DPoPToken,
  OAuth2AccessTokenAuth,
  OAuth2Client,
  TokenSerializer,
)

import oauth_dropins.bluesky

import app as app_module
from app import app, BlueskyCallback, BlueskyStart, Feed

SESSION = {'accessJwt': 'towkin', 'refreshJwt': 'reephrush'}
PROFILE = {
    '$type': 'app.bsky.actor.defs#profileViewDetailed',
    'did': 'did:plc:alice',
    'handle': 'alice.bsky.social',
}
DPOP_TOKEN = DPoPToken(access_token='towkin', _dpop_key=DPoPKey.generate())
DPOP_TOKEN_STR = TokenSerializer().dumps(DPOP_TOKEN)


class TestCase(unittest.TestCase):
    def setUp(self):
        requests.post(f'http://{ndb_client.host}/reset')

        self.ndb_ctx = ndb_client.context()
        self.ndb_ctx.__enter__()

        app.testing = True
        self.client = app.test_client()
        self.client.__enter__()

    def tearDown(self):
        self.ndb_ctx.__exit__(None, None, None)
        self.client.__exit__(None, None, None)


class BlueskyAtomTest(TestCase):
    def setUp(self):
        super().setUp()

        self.auth = BlueskyAuth(id='did:plc:alice', pds_url='https://bsky.social',
                                user_json=json_dumps(PROFILE), session=SESSION)
        self.auth.put()

        self.feed = Feed(handle='alice.bsky.social', session={'did': 'did:plc:alice'})
        self.feed.put()

    def test_home(self):
        resp = self.client.get('/')
        self.assertEqual(200, resp.status_code)

    def test_redirect_url_state_from_checkboxes(self):
        with patch.object(oauth_dropins.bluesky.OAuthStart, 'redirect_url',
                          return_value='https://bsky.social/authorize') as mock_redirect:
            self.client.post('/oauth/bluesky/start', data={
                'handle': 'alice.bsky.social',
                'replies': 'on',
                'reposts': 'on',
            })
        mock_redirect.assert_called_once_with(state='replies=true&reposts=true',
                                              handle=None)

    def test_redirect_url_no_state_when_unchecked(self):
        with patch.object(oauth_dropins.bluesky.OAuthStart, 'redirect_url',
                          return_value='https://bsky.social/authorize') as mock_redirect:
            self.client.post('/oauth/bluesky/start',
                             data={'handle': 'alice.bsky.social'})
        mock_redirect.assert_called_once_with(state=None, handle=None)

    def test_client_metadata(self):
        resp = self.client.get('/oauth/client-metadata.json')
        self.assertEqual(200, resp.status_code)
        self.assertEqual({
            'application_type': 'web',
            'client_id': 'http://localhost/oauth/client-metadata.json',
            'client_name': 'bluesky-atom',
            'client_uri': 'http://localhost/',
            'dpop_bound_access_tokens': True,
            'grant_types': [
                'authorization_code',
                'refresh_token',
            ],
            'redirect_uris': ['http://localhost/oauth/bluesky/callback'],
            'response_types': ['code'],
            'scope': 'atproto transition:generic',
            'token_endpoint_auth_method': 'none',
        }, resp.get_json())

    def test_finish_generates_feed_url(self):
        with patch.object(BlueskyCallback, 'dispatch_request',
                          lambda cb: cb.finish(self.auth, state='')):
            resp = self.client.get('/oauth/bluesky/callback')
        self.assertEqual(200, resp.status_code)
        self.assertIn(f'feed_id={self.feed.key.id()}', resp.get_data(as_text=True))

    def test_finish_state_replies_reposts(self):
        with patch.object(BlueskyCallback, 'dispatch_request',
                          lambda cb: cb.finish(self.auth, state='replies=true&reposts=true')):
            resp = self.client.get('/oauth/bluesky/callback')
        self.assertEqual(200, resp.status_code)
        body = resp.get_data(as_text=True)
        self.assertIn('replies=true', body)
        self.assertIn('reposts=true', body)

    def test_finish_declined(self):
        with patch.object(BlueskyCallback, 'dispatch_request',
                          lambda cb: cb.finish(None, state='')):
            resp = self.client.get('/oauth/bluesky/callback')
        self.assertEqual(200, resp.status_code)
        self.assertIn('declined', resp.get_data(as_text=True))

    @patch.object(Bluesky, 'get_activities', return_value=[{
        'objectType': 'activity',
        'verb': 'post',
        'object': {'objectType': 'note', 'content': 'hello'},
    }])
    def test_feed(self, _):
        resp = self.client.get(f'/feed?feed_id={self.feed.key.id()}')
        self.assertEqual(200, resp.status_code)
        self.assertIn('application/atom+xml', resp.content_type)
        self.assertIn(b'hello', resp.data)

    def test_feed_not_found(self):
        resp = self.client.get('/feed?feed_id=9999')
        self.assertEqual(400, resp.status_code)

    @patch.object(Bluesky, 'get_activities', return_value=[{
        'objectType': 'activity',
        'verb': 'post',
        'object': {'objectType': 'comment', 'content': 'a reply'},
    }, {
        'objectType': 'activity',
        'verb': 'share',
        'object': {'objectType': 'note', 'content': 'reposted'},
    }, {
        'objectType': 'activity',
        'verb': 'post',
        'object': {'objectType': 'note', 'content': 'top-level'},
    }])
    def test_feed_filters_replies_and_reposts(self, _):
        resp = self.client.get(f'/feed?feed_id={self.feed.key.id()}')
        self.assertEqual(200, resp.status_code)
        body = resp.get_data(as_text=True)
        self.assertNotIn('a reply', body)
        self.assertNotIn('reposted', body)
        self.assertIn('top-level', body)

    @patch.object(Bluesky, 'get_activities', return_value=[{
        'objectType': 'activity',
        'verb': 'post',
        'object': {'objectType': 'comment', 'content': 'a reply'},
    }])
    def test_feed_include_replies(self, _):
        resp = self.client.get(f'/feed?feed_id={self.feed.key.id()}&replies=true')
        self.assertEqual(200, resp.status_code)
        self.assertIn('a reply', resp.get_data(as_text=True))

    @patch.object(Bluesky, 'get_activities', return_value=[{
        'objectType': 'activity',
        'verb': 'share',
        'object': {'objectType': 'note', 'content': 'reposted'},
    }])
    def test_feed_include_reposts(self, _):
        resp = self.client.get(f'/feed?feed_id={self.feed.key.id()}&reposts=true')
        self.assertEqual(200, resp.status_code)
        self.assertIn('reposted', resp.get_data(as_text=True))

    def test_session_callback_updates_session(self):
        new_session = {'accessJwt': 'new towkin', 'refreshJwt': 'new reephrush'}
        auth = BlueskyAuth.get_by_id('did:plc:alice')
        bluesky = app_module.get_client(auth)
        bluesky._client.session_callback(new_session)
        auth = BlueskyAuth.get_by_id('did:plc:alice')
        self.assertEqual(new_session, auth.session)

    # TODO: test OAuth callback

    @patch('oauth_dropins.bluesky.oauth_client_for_pds')
    @patch.object(Bluesky, 'get_activities', return_value=[{
        'objectType': 'activity',
        'verb': 'post',
        'object': {'objectType': 'note', 'content': 'oauth post'},
    }])
    def test_feed_oauth_user(self, _, __):
        self.auth.dpop_token = DPOP_TOKEN_STR
        self.auth.session = None
        self.auth.put()

        resp = self.client.get(f'/feed?feed_id={self.feed.key.id()}')
        self.assertEqual(200, resp.status_code)
        self.assertIn(b'oauth post', resp.data)


if __name__ == '__main__':
    unittest.main()
