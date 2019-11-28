#
# std library
#
import logging, threading, signal

#
# 3rd party
#
import bottle, clize

#
# local
#
import spotutil

logger = logging.getLogger(__name__)

VERSION = "2.0.1"

#
# Globals
#

BYTES_PER_SECOND = 2 * 2 * 44100

class Status:
    def __init__(self):
        self.streaming = self.streamed = False
        self.uri = None
        self.error = None
        self.bytes = 0
    def reset(self, uri):
        self.streaming = self.streamed = False
        self.uri = uri
        self.error = None
        self.bytes = 0
    def to_dict(self):
        return self.__dict__.copy()


app = bottle.Bottle()                # the global app
status = Status()

def main(*, port=39705, server='waitress', debug=False):
    if debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARN)

    spotutil.start()
    print("Spotweb v%s" % VERSION)
    bottle.run(app, server=server, port=port)

#
# Main Routing Entry Points
#

@app.route('/album/<album_id>')
def album_details(album_id):
    uri = _expand_uri('spotify:album:', album_id)
    return format_album(spotutil.session.get_album(uri))

@app.route('/track/<track_id>')
def track_details(track_id):
    uri = _expand_uri('spotify:track:', track_id)
    return format_track(spotutil.session.get_track(uri))

@app.route('/play/<track_id>')
def play_track(track_id):
    uri = _expand_uri('spotify:track:', track_id)
    if status.streaming: bottle.abort(403)
    player = spotutil.Player(uri)
    if player.track.availability != 1: bottle.abort(403)
    response = bottle.response
    response.content_type = 'audio/x-pcm'
    status.reset(uri)
    try:
        status.streaming = True
        logger.debug('streaming started')
        for data in player.get_data():
            status.bytes += len(data)
            yield data
        status.streamed = True
    except spotutil.PlayError as e:
        logger.debug('Play error received', e)
        logger.error(str(e))
        status.error = str(e)
    except Exception as e:
        logger.debug('Exception received', e)
        logger.error(str(e))
        status.error = str(e)
        raise
    finally:
        logger.debug('streaming stopped')
        player.stop() # stop it regardless
        status.streaming = False

@app.route('/status')
def send_status():
    return status.to_dict()

#
# formatting
#

def format_album(a):
    br = a.browse()
    br.load()
    a.load()
    a.artist.load()
    tracks = []
    data = dict(uri=str(a.link), name=a.name, year=a.year,
                artist=dict(uri=str(a.artist.link), name=a.artist.name),
                tracks=tracks)
    for t in br.tracks:
        tracks.append(format_track(t))
    return data

def format_track(t):
    t.load()
    artists = []
    data = dict(uri=str(t.link), name=t.name, duration=t.duration,
                disc=t.disc, number=t.index, album=str(t.album.link),
                artists=artists)
    for ta in t.artists:
        ta.load()
        artists.append(dict(uri=str(ta.link), name=ta.name))
    return data

#
# Utilities
#

def _expand_uri(prefix, uri):
    if uri.startswith(prefix): return uri
    return prefix + uri


if __name__ == '__main__':
    clize.run(main)
