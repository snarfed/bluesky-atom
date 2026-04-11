"""Tests for app.py."""
import unittest
from unittest.mock import patch

from granary.bluesky import Bluesky
from granary.tests.test_bluesky import (
    POST_FEED_VIEW_BSKY,
    REPLY_POST_VIEW_BSKY,
    REPOST_BSKY_FEED_VIEW_POST,
)
from oauth_dropins.bluesky import BlueskyAuth, OAuthStart
from oauth_dropins.webutil.appengine_config import ndb_client
from oauth_dropins.webutil.testutil import (
    Asserts,
    requests_response,
    suppress_warnings,
)
from oauth_dropins.webutil.util import json_dumps
import requests
from requests_oauth2client import (
  DPoPKey,
  DPoPToken,
  OAuth2AccessTokenAuth,
  OAuth2Client,
  TokenSerializer,
)

from app import app, BlueskyCallback, BlueskyStart, Feed

DPOP_TOKEN = DPoPToken(access_token='towkin', _dpop_key=DPoPKey.generate())
DPOP_TOKEN_STR = TokenSerializer().dumps(DPOP_TOKEN)

POST_FEED_VIEW_BSKY['post']['cid'] = 'bafyfoobarbazbiff'
REPOST_BSKY_FEED_VIEW_POST['post']['cid'] = 'bafyfoobarbazbiff'
REPLY_BSKY_FEED_VIEW_POST = {
  '$type': 'app.bsky.feed.defs#feedViewPost',
  'post': {
      **REPLY_POST_VIEW_BSKY,
      'cid': 'bafy1234567890',
      'author': {
          '$type': 'app.bsky.actor.defs#profileViewBasic',
          'did': 'did:al:ice',
          'handle': 'alice.net',
      }
  },
}

ATOM = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xml:lang="en-US"
      xmlns="http://www.w3.org/2005/Atom"
      xmlns:activity="http://activitystrea.ms/spec/1.0/"
      xmlns:georss="http://www.georss.org/georss"
      xmlns:ostatus="http://ostatus.org/schema/1.0"
      xmlns:thr="http://purl.org/syndication/thread/1.0"
      xml:base="https://bsky.app">
<generator uri="https://granary.io/">granary</generator>
<id>http://localhost/</id>
<title>bluesky-atom feed</title>
<updated>2007-07-07T03:04:05+00:00</updated>
<author>
 <activity:object-type>http://activitystrea.ms/schema/1.0/person</activity:object-type>
 <uri></uri>
</author>
<link rel="alternate" href="http://localhost/" type="text/html" />
<link rel="self" href="http://localhost/feed?feed_id=123" type="application/atom+xml" />
<entry>
<author>
 <activity:object-type>http://activitystrea.ms/schema/1.0/person</activity:object-type>
 <uri>https://bsky.app/profile/alice.com</uri>
 <name>Alice</name>
</author>
    <activity:object-type>http://activitystrea.ms/schema/1.0/note</activity:object-type>
  <id>at://did:al:ice/app.bsky.feed.post/tid</id>
  <title>My original post</title>
  <content type="html"><![CDATA[
My original post
  ]]></content>
  <link rel="alternate" type="text/html" href="https://bsky.app/profile/alice.com/post/tid" />
  <link rel="ostatus:conversation" href="https://bsky.app/profile/alice.com/post/tid" />
    <activity:verb>http://activitystrea.ms/schema/1.0/post</activity:verb>
  <published>2007-07-07T03:04:05+00:00</published>
  <updated>2007-07-07T03:04:05+00:00</updated>
  <link rel="self" href="https://bsky.app/profile/alice.com/post/tid" />
</entry>
</feed>
"""


class BlueskyAtomTest(unittest.TestCase, Asserts):
    def setUp(self):
        super().setUp()
        suppress_warnings()

        requests.post(f'http://{ndb_client.host}/reset')

        self.ndb_context = ndb_client.context()
        self.ndb_context.__enter__()

        app.testing = True
        self.client = app.test_client()
        self.client.__enter__()

        self.auth = BlueskyAuth(id='did:plc:alice', pds_url='https://pds.com',
                                user_json=json_dumps({
                                    '$type': 'app.bsky.actor.defs#profileViewDetailed',
                                    'did': 'did:plc:alice',
                                    'handle': 'alice.net',
                                }))
        self.auth.put()
        self.feed = Feed(id=123, handle='alice.net', session={'did': 'did:plc:alice'})
        self.feed.put()

        Feed.bluesky.cache_clear(self.feed)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.ndb_context.__exit__(None, None, None)

    def test_home(self):
        resp = self.client.get('/')
        self.assertEqual(200, resp.status_code)

    @patch.object(OAuthStart, 'redirect_url', return_value='https://pds.com/auth')
    def test_redirect_url_state_from_checkboxes(self, mock_redirect_url):
        self.client.post('/oauth/bluesky/start', data={
            'handle': 'alice.bsky.social',
            'replies': 'on',
            'reposts': 'on',
        })
        mock_redirect_url.assert_called_once_with(state='replies=true&reposts=true',
                                                  handle=None)

    @patch.object(OAuthStart, 'redirect_url', return_value='https://pds.com/auth')
    def test_redirect_url_no_state_when_unchecked(self, mock_redirect_url):
        self.client.post('/oauth/bluesky/start', data={'handle': 'alice.bsky.social'})
        mock_redirect_url.assert_called_once_with(state=None, handle=None)

    def test_client_metadata(self):
        resp = self.client.get('/oauth/client-metadata.json')
        self.assertEqual(200, resp.status_code)
        self.assertEqual({
            'application_type': 'web',
            'client_id': 'http://localhost/oauth/client-metadata.json',
            'client_name': 'bluesky-atom',
            'client_uri': 'http://localhost/',
            'dpop_bound_access_tokens': True,
            'grant_types': ['authorization_code', 'refresh_token'],
            'redirect_uris': ['http://localhost/oauth/bluesky/callback'],
            'response_types': ['code'],
            'scope': 'atproto transition:generic',
            'token_endpoint_auth_method': 'none',
        }, resp.get_json())

    def test_oauth_finish_generate_feed_url(self):
        with app.test_request_context('/'):
            ret = BlueskyCallback('-').finish(self.auth, state=None)
        self.assertIn('/feed?feed_id=123"', ret)

    def test_oauth_finish_generate_feed_url_with_replies_reposts(self):
        with app.test_request_context('/'):
            ret = BlueskyCallback('-').finish(self.auth, state='replies=true&reposts=true')
        self.assertIn('/feed?feed_id=123&replies=true&reposts=true"', ret)

    def test_finish_declined(self):
        with patch.object(BlueskyCallback, 'dispatch_request',
                          lambda cb: cb.finish(None, state='')):
            resp = self.client.get('/oauth/bluesky/callback')
        self.assertEqual(200, resp.status_code)
        self.assertIn('declined', resp.get_data(as_text=True))

    @patch('oauth_dropins.bluesky.oauth_client_for_pds',
           return_value=OAuth2Client(token_endpoint='https://un/used',
                                     client_id='unused', client_secret='unused'))
    @patch('requests.get', return_value=requests_response({
      'feed': [POST_FEED_VIEW_BSKY, REPOST_BSKY_FEED_VIEW_POST],
    }))
    def test_feed_dpop(self, mock_get, _):
        self.auth.dpop_token = DPOP_TOKEN_STR
        self.auth.put()

        resp = self.client.get('/feed?feed_id=123')
        self.assertEqual(200, resp.status_code)
        self.assertEqual('application/atom+xml', resp.content_type)
        self.assert_multiline_equals(ATOM, resp.data.decode(), ignore_blanks=True)
        self.assertEqual(DPOP_TOKEN, mock_get.call_args.kwargs['auth'].token)

    @patch('requests.get', return_value=requests_response({
      'feed': [POST_FEED_VIEW_BSKY, REPOST_BSKY_FEED_VIEW_POST],
    }))
    def test_feed_dpop_session(self, mock_get):
        self.auth.session = {'accessJwt': 'towkin', 'refreshJwt': 'reephrush'}
        self.auth.put()

        resp = self.client.get('/feed?feed_id=123')
        self.assertEqual(200, resp.status_code)
        self.assertEqual('application/atom+xml', resp.content_type)
        self.assert_multiline_equals(ATOM, resp.data.decode(), ignore_blanks=True)
        self.assertEqual('Bearer towkin',
                         mock_get.call_args.kwargs['headers']['Authorization'])

    def test_feed_not_found(self):
        resp = self.client.get('/feed?feed_id=9999')
        self.assertEqual(400, resp.status_code)

    @patch('requests.get', return_value=requests_response({
      'feed': [REPLY_BSKY_FEED_VIEW_POST, REPOST_BSKY_FEED_VIEW_POST],
    }))
    def test_feed_with_replies_reposts(self, mock_get):
        resp = self.client.get('/feed?feed_id=123&replies=true&reposts=true')
        self.assertEqual(200, resp.status_code)
        body = resp.get_data(as_text=True)
        self.assertIn(REPLY_BSKY_FEED_VIEW_POST['post']['record']['text'], body)
        self.assertIn(REPOST_BSKY_FEED_VIEW_POST['post']['record']['text'], body)
