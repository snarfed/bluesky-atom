"""Fetches Bluesky timeline, converts it to Atom, and serves it."""
import datetime
import logging

from cachetools import cached, LRUCache
from flask import Flask, request
from flask_caching import Cache
import flask_gae_static
from granary import atom, bluesky
from granary.bluesky import Bluesky
from oauth_dropins.webutil import appengine_config, appengine_info, flask_util, util

CACHE_EXPIRATION = datetime.timedelta(minutes=15)

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


# cache access tokens in Bluesky instances
# TODO: catch errors below and refresh when tokens expire
# XRPC will return 400 with JSON body {'error': 'ExpiredToken'}
# https://github.com/jesopo/bisky/blob/ed2977f75db1a7fa89f0db3d9e795d37a7f48485/src/atproto.rs#L224
@cached(LRUCache(1000))
def bluesky_instance(**kwargs):
  return bluesky.Bluesky(**kwargs)


@app.route('/feed')
@flask_util.cached(Cache(app), CACHE_EXPIRATION)
def feed():
  bs = bluesky_instance(handle=flask_util.get_required_param('handle'),
                        app_password=flask_util.get_required_param('password'))
  activities = bs.get_activities()
  logging.info(f'Got {len(activities)} activities')

  # Generate output
  return atom.activities_to_atom(
    activities, {}, title='bluesky-atom feed',
    host_url=request.host_url,
    request_url=request.url,
    xml_base=Bluesky.BASE_URL,
  ), {'Content-Type': 'application/atom+xml'}
