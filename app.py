"""Fetches Bluesky timeline, converts it to Atom, and serves it."""
import logging
from urllib.parse import parse_qsl, urljoin

from flask import Flask, render_template, request
import flask_gae_static
from google.cloud import ndb
from granary import as1, atom
from granary.bluesky import Bluesky
import oauth_dropins.bluesky
from oauth_dropins.bluesky import BlueskyAuth
from oauth_dropins.webutil import appengine_config, appengine_info, flask_util, util
from oauth_dropins.webutil.models import JsonProperty
from oauth_dropins.webutil.util import json_loads
from requests_oauth2client import OAuth2AccessTokenAuth, TokenSerializer

DOMAIN = 'bluesky-atom.appspot.com'

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
if appengine_info.DEBUG or appengine_info.LOCAL_SERVER:
    flask_gae_static.init_app(app)
app.wsgi_app = flask_util.ndb_context_middleware(
    app.wsgi_app, client=appengine_config.ndb_client)


class Feed(ndb.Model):
    handle = ndb.StringProperty(required=True)
    password = ndb.StringProperty()
    session = JsonProperty(default={})


def client_metadata():
    base = (request.host_url if appengine_info.DEBUG or appengine_info.LOCAL_SERVER
            else f'https://{DOMAIN}/')
    return {
        **oauth_dropins.bluesky.CLIENT_METADATA_TEMPLATE,
        'client_id': urljoin(base, '/oauth/client-metadata.json'),
        'client_name': 'bluesky-atom',
        'client_uri': base,
        'redirect_uris': [urljoin(base, '/oauth/bluesky/callback')],
    }


class BlueskyStart(oauth_dropins.bluesky.OAuthStart):
    @property
    def CLIENT_METADATA(self):
        return client_metadata()

    def redirect_url(self, state=None, handle=None):
        parts = []
        if request.values.get('replies'):
            parts.append('replies=true')
        if request.values.get('reposts'):
            parts.append('reposts=true')
        return super().redirect_url(state='&'.join(parts) or None, handle=handle)


class BlueskyCallback(oauth_dropins.bluesky.OAuthCallback):
    @property
    def CLIENT_METADATA(self):
        return client_metadata()

    def dispatch_request(self):
        try:
            return super().dispatch_request()
        except ValueError as e:
            return render_template('index.html', error=str(e))

    def finish(self, auth, state=None):
        if not auth:
            return render_template('index.html', error='Login declined or failed')

        handle = json_loads(auth.user_json)['handle']
        if not (feed := Feed.query(Feed.handle == handle).get()):
            feed = Feed(handle=handle)
            feed.put()

        feed_url = urljoin(request.host_url, f'/feed?feed_id={feed.key.id()}&{state}')
        return render_template('index.html', feed_url=feed_url)


def get_client(auth_entity):
    did = auth_entity.key.id()
    handle = json_loads(auth_entity.user_json).get('handle')
    pds_url = auth_entity.pds_url or 'https://bsky.social'

    if auth_entity.dpop_token:  # OAuth
        def callback(auth):
            serialized = TokenSerializer().dumps(auth.token)
            if serialized != auth_entity.dpop_token:
                logging.info(f'Storing DPoP token for {auth_entity.key.id()}')
                auth_entity.dpop_token = serialized
                auth_entity.put()

        oauth_client = oauth_dropins.bluesky.oauth_client_for_pds(
            client_metadata(), pds_url)
        token = TokenSerializer().loads(auth_entity.dpop_token)
        return Bluesky(handle=handle, pds_url=pds_url, session_callback=callback,
                       auth=OAuth2AccessTokenAuth(client=oauth_client, token=token))

    else:  # app password based
        def callback(session):
            if session != auth_entity.session:
                logging.info(f'Storing session for {auth_entity.key.id()}: {session}')
                auth_entity.session = session
                auth_entity.put()

        assert auth_entity.session
        return Bluesky(handle=handle, pds_url=pds_url, session_callback=callback,
                       access_token=auth_entity.session.get('accessJwt'),
                       refresh_token=auth_entity.session.get('refreshJwt'))


def get_bool_param(name):
    val = request.values.get(name)
    return val and val.strip().lower() not in ['false', 'no', 'off']


@app.get('/')
@flask_util.headers({'Cache-Control': 'public, max-age=86400'})
def home():
    html = BlueskyStart.button_html('/oauth/bluesky/start',
                                    image_prefix='/oauth_dropins_static/',
                                    form_extra="""\
<input type="checkbox" id="replies" name="replies" checked="checked" />
<label for="replies">Include replies</label>&nbsp;&nbsp;
<input type="checkbox" id="reposts" name="reposts" checked="checked" />
<label for="reposts">Include reposts</label>&nbsp;&nbsp;
""")
    return render_template('index.html', bluesky_button=html)


@app.get('/oauth/client-metadata.json')
@flask_util.headers({'Cache-Control': 'public, max-age=3600'})
def bluesky_client_metadata():
    """https://docs.bsky.app/docs/advanced-guides/oauth-client#client-and-server-metadata"""
    return client_metadata()


@app.get('/feed')
@flask_util.headers({'Cache-Control': 'public, max-age=300'})
def feed():
    feed_id = flask_util.get_required_param('feed_id').strip()
    if not util.is_int(feed_id):
        flask_util.error(f'Expected integer feed_id; got {feed_id}')

    if (not (feed := Feed.get_by_id(int(feed_id)))
            or not (did := feed.session.get('did'))):
        flask_util.error(f'Feed {feed_id} not found')

    if not (auth := BlueskyAuth.get_by_id(did)):
        flask_util.error(f'User {did} not found')

    activities = []
    for a in get_client(auth).get_activities():
        type = as1.object_type(a)
        if type in ('post', 'update'):
            type = as1.object_type(as1.get_object(a))
        if ((get_bool_param('replies') or type != 'comment')
            and (get_bool_param('reposts') or type != 'share')):
            activities.append(a)
    logging.info(f'Got {len(activities)} activities')

    return atom.activities_to_atom(
        activities, {}, title='bluesky-atom feed',
        host_url=request.host_url,
        request_url=request.url,
        xml_base=Bluesky.BASE_URL,
    ), {'Content-Type': 'application/atom+xml'}


app.add_url_rule('/oauth/bluesky/start',
                 view_func=BlueskyStart.as_view('/oauth/bluesky/start',
                                                '/oauth/bluesky/callback'),
                 methods=['POST'])
app.add_url_rule('/oauth/bluesky/callback',
                 view_func=BlueskyCallback.as_view('/oauth/bluesky/callback', '/'))
