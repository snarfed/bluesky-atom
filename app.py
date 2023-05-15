"""Fetches Bluesky timeline, converts it to Atom, and serves it."""
import datetime
import logging
from urllib.parse import urljoin

from cachetools import cachedmethod, TTLCache
from cachetools.keys import hashkey
from flask import Flask, render_template, request
from flask_caching import Cache
import flask_gae_static
from google.cloud import ndb
from granary import as1, atom, bluesky
from granary.bluesky import Bluesky
from oauth_dropins.webutil import appengine_config, appengine_info, flask_util, util
from requests.exceptions import HTTPError

CACHE_EXPIRATION = datetime.timedelta(minutes=5)
# access tokens currently expire in 2h, refresh tokens expire in 90d
# https://github.com/bluesky-social/atproto/blob/5b0c2d7dd533711c17202cd61c0e101ef3a81971/packages/pds/src/auth.ts#L46
# https://github.com/bluesky-social/atproto/blob/5b0c2d7dd533711c17202cd61c0e101ef3a81971/packages/pds/src/auth.ts#L65
TOKEN_EXPIRATION = datetime.timedelta(hours=2)

# Flask app
app = Flask('bluesky-atom', static_folder=None)
app.template_folder = './templates'
app.config.from_mapping(
    ENV='development' if appengine_info.DEBUG else 'production',
    CACHE_TYPE='NullCache' if appengine_info.DEBUG else 'SimpleCache',
    SECRET_KEY=util.read('flask_secret_key'),
)
app.after_request(flask_util.default_modern_headers)
app.register_error_handler(Exception, flask_util.handle_exception)
if appengine_info.DEBUG:
    flask_gae_static.init_app(app)
app.wsgi_app = flask_util.ndb_context_middleware(
    app.wsgi_app, client=appengine_config.ndb_client)

request_cache = Cache(app)
bluesky_cache = TTLCache(maxsize=1000, ttl=TOKEN_EXPIRATION.total_seconds())


class Feed(ndb.Model):
    handle = ndb.StringProperty(required=True)
    password = ndb.StringProperty(required=True)

    # cache Bluesky instances to reuse access tokens until they expire
    # TODO: catch errors below and refresh when tokens expire?
    # XRPC will return 400 with JSON body {'error': 'ExpiredToken'}
    # https://github.com/jesopo/bisky/blob/ed2977f75db1a7fa89f0db3d9e795d37a7f48485/src/atproto.rs#L224
    @cachedmethod(lambda self: bluesky_cache,
                  key=lambda self: hashkey(self.handle, self.password))
    def bluesky(self):
        return bluesky.Bluesky(handle=self.handle, app_password=self.password)


def get_bool_param(name):
    val = request.values.get(name)
    return val and val.strip().lower() not in ['false', 'no', 'off']


@app.get('/')
@flask_util.cached(request_cache, datetime.timedelta(days=1))
def home():
    return render_template('index.html')


@app.get('/feed')
@flask_util.cached(request_cache, CACHE_EXPIRATION)
def feed():
    feed_id = flask_util.get_required_param('feed_id').strip()
    if not util.is_int(feed_id):
        flask_util.error(f'Expected integer feed_id; got {feed_id}')

    feed = Feed.get_by_id(int(feed_id))
    activities = [a for a in feed.bluesky().get_activities()
                  if (get_bool_param('replies') or as1.object_type(a) != 'comment')
                  and (get_bool_param('reposts') or as1.object_type(a) != 'share')]
    logging.info(f'Got {len(activities)} activities')

    # special case myself to beta test bsky.app URL deep links into phone apps
    # based on granary.as1.prefix_urls()
    if feed.handle == 'snarfed.org':
        def _update_urls(a):
            for elem in ([a, a.get('object'), a.get('author'), a.get('actor')] +
                         a.get('replies', {}).get('items', []) +
                         a.get('attachments', []) + a.get('tags', [])):
                if elem:
                    url = elem.get('url')
                    if url and url.startswith('https://staging.bsky.app/'):
                        elem['url'] = url.replace('https://staging.bsky.app',
                                                  'https://bsky.app')
                    if elem is not a:
                        _update_urls(elem)

        for a in activities:
            _update_urls(a)

    # Generate output
    return atom.activities_to_atom(
        activities, {}, title='bluesky-atom feed',
        host_url=request.host_url,
        request_url=request.url,
        xml_base=Bluesky.BASE_URL,
    ), {'Content-Type': 'application/atom+xml'}


@app.post('/generate')
def generate():
    handle = flask_util.get_required_param('handle').strip()
    password = flask_util.get_required_param('password').strip()

    feed = Feed.query(Feed.handle == handle, Feed.password == password).get()
    if not feed:
        feed = Feed(handle=handle, password=password)
        try:
            feed.bluesky()
        except HTTPError as e:
            try:
                resp = e.response.json()
                msg = resp.get('message') or resp.get('error') or str(e)
            except ValueError:
                msg = str(e)
            return render_template('index.html', error=msg), 502
        feed.put()

    params = {'feed_id': feed.key.id()}
    for param in 'replies', 'reposts':
        if get_bool_param(param):
            params[param] = 'true'

    feed_url = util.add_query_params(urljoin(request.host_url, '/feed'), params)
    return render_template('index.html', feed_url=feed_url, request=request)
