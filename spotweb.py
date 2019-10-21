#
# std library
#
import logging, subprocess, threading, collections, time, signal

#
# 3rd party
#
import bottle, clize
from bunch import *

#
# local
#
import spotutil

logger = logging.getLogger(__name__)

VERSION = "1.0.1"

#
# Globals
#

app = bottle.Bottle()                # the global app
status = Bunch(streaming=False)  # current server status
receipts = collections.OrderedDict()
receiptPeriod = 12 * 3600   # keep receipts for how long (12 hours)



def main(*, port=39704, debug=False, timeout=300):

    if debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARN)

    spotutil.start()

    # from https://gist.github.com/coffeesnake/3093598
    from wsgiref.simple_server import make_server, WSGIServer
    from socketserver import ThreadingMixIn
    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer): pass

    httpd = make_server('0.0.0.0', port, app, ThreadingWSGIServer)

    def shutdown(signum, frame):
        # run shutdown from another thread
        threading.Thread(target=httpd.shutdown).start()
        print("Server shut down")

    signal.signal(signal.SIGTERM, shutdown)
    #threading.Thread(target=idle_thread, args=(httpd, timeout)).start()
    print("Spotweb v%s" % VERSION)
    print('Listening on port %d....' % port)
    httpd.serve_forever()



#
# Main Routing Entry Points
#

@app.route('/album/<album_id>')
def album_details(album_id):
    uri = _expand_uri('spotify:album:', album_id)
    logger.debug('Getting %s', uri)
    a = get_album_details(uri)
    return unbunchify(a)

@app.route('/cover/<album_id>')
def album_cover(album_id):
    uri = _expand_uri('spotify:album:', album_id)
    logger.debug('Getting cover for %s', uri)
    bottle.response.content_type = 'image/jpeg'
    return get_album_cover(uri)

@app.route('/play/<track_id>')
def play_track(track_id):
    uri = _expand_uri('spotify:track:', track_id)
    if status.streaming: bottle.abort(403)
    request = bottle.request
    response = bottle.response
    fmt = request.query.format or 'flac'
    rcpt = make_receipt(uri)
    if fmt == 'flac':
        response.content_type = 'audio/flac'
        rcpt.format = 'flac'
    elif fmt == 'raw':
        response.content_type = 'audio/x-pcm'
        rcpt.format = 'raw'
    else:
        bottle.abort(415)
    data = get_track_pcm(uri, rcpt)
    if fmt == 'flac':
        data = flac_encode(data, rcpt)
    yield from data
    logger.debug("Final receipt=%r", rcpt)

@app.route('/receipt/<track_id>')
def send_receipt(track_id):
    uri = _expand_uri('spotify:track:', track_id)
    if not uri in receipts: bottle.abort(404)
    return unbunchify(receipts[uri])

@app.route('/status')
def send_status():
    s = Bunch(status=status, receipts=receipts)
    return unbunchify(s)

#
# Playing routines
#

def get_track_pcm(uri, rcpt):
    logger.debug('streaming started')
    player = spotutil.Player(uri)
    # throw 403 if track not playable
    if player.track.availability != 1: bottle.abort(403)
    status.streaming = True
    status.uri = uri
    status.length = player.track.duration // 1000
    status.pos = 0
    def notify(n):
        m, s = divmod(n // 44100, 60)
        logger.info("Got %02d:%02d of music",m,s)
    player.set_notify(notify, 10*44100)

    rcpt.pcm = 0
    try:
        for data in player.get_data():
            rcpt.pcm += len(data)
            status.pos = rcpt.pcm // (2 * 2 * 44100)
            yield data
        rcpt.streamed = True
    except spotutil.PlayError as e:
        logger.error(str(e))
        rcpt.failed = True
        rcpt.error = str(e)
    except Exception as e:
        logger.error(str(e))
        rcpt.failed = True
        rcpt.error = str(e)
        raise
    finally:
        logger.debug('streaming stopped')
        player.stop() # stop it regardless
        rcpt.end = time.time()
        status.streaming = False

def flac_encode(input_data, rcpt, *, blksize=8192):
    try:
        logger.debug('going to encode via flac')
        flac = subprocess.Popen([
            'flac', '--silent', '--force',
            '--stdout',
            '--force-raw-format', '--endian=little',
            '--channels=2', '--bps=16', '--sample-rate=44100', '--sign=signed',
            '-' ], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        rcpt.flac = 0
        feeder = threading.Thread(target=feed_child, daemon=True,
                    args=(input_data, flac.stdin))
        feeder.start()
        while True:
            data = flac.stdout.read(blksize)
            if not data: break
            rcpt.flac += len(data)
            yield data
        flac.stdout.close()
        feeder.join()
        flac.wait()
        logger.debug('After flac encode, rcpt=%r', rcpt)
    except Exception as e:
        logger.error(str(e))
        raise


def feed_child(source, child):
    try:
        for data in source:
            child.write(data)
    except Exception as e:
        logger.err(str(e))
        raise
    finally:
        child.close()
        logger.debug('finished feeding')

#
# Spotify metadata
#

def get_album_details(uri, artist=True,
        tracks=True, track_artists=True):

    alb = spotutil.session.get_album(uri)
    br = alb.browse()
    br.load()
    alb.load()
    logger.debug('loaded')
    a = Bunch()
    a.uri = uri
    a.name = alb.name
    # include artist as link or object?
    if not artist:
        a.artist = str(alb.artist.link)
    else:
        artist = a.artist = Bunch()
        artist.uri = str(alb.artist.link)
        artist.name = alb.artist.load().name
    a.year = alb.year
    a.tracks = []
    for t in br.tracks:
        # just include track links
        if not tracks:
            a.tracks.append(str(t.link))
        else:
            trk = Bunch()
            a.tracks.append(trk)
            t.load()
            trk.uri = str(t.link)
            trk.name = t.name
            trk.duration = t.duration
            trk.disc = t.disc
            trk.number = t.index
            artists = trk.artists = []
            for ta in t.artists:
                if not track_artists:
                    artists.append(str(ta.link))
                else:
                    artist = Bunch()
                    artists.append(artist)
                    ta.load()
                    artist.uri = str(ta.link)
                    artist.name = ta.name
    return a

def get_album_cover(uri):
    alb = spotutil.session.get_album(uri)
    if not alb.is_loaded:
        br = alb.browse()
        br.load()
        alb.load()
        logger.debug('loaded')
    if not alb.is_loaded:
        logger.debug('still not loaded')
    cover = alb.cover()
    if cover is None: bottle.abort(404)
    logger.debug('loading cover')
    cover.load()
    logger.debug('loaded cover')
    logger.debug(cover.error)
    logger.debug(cover.data)
    return cover.data

#
# Utilities
#


def _expand_uri(prefix, uri):
    if uri.startswith(prefix): return uri
    return prefix + uri


def make_receipt(code):
    if code in receipts:
        del receipts[code]
    now = time.time()
    # clear out old
    while True:
        first_rcpt = next(iter(receipts), None)
        if not first_rcpt: break
        if 'end' not in first_rcpt: break
        if first_rcpt.end > (now - receiptPeriod): break
        receipts.popitem(last=False)
    rcpt = Bunch()
    rcpt.start = now
    receipts[code] = rcpt
    return rcpt

#def busy():
#    # marks status as busy
#    status.lastActive = time.time()
#    logger.debug("Marked as not idle")
#
#def idle_thread(server, timeout):
#    status.lastActive = time.time()
#    logger.debug("Will shutdown after %d seconds idle")
#    while True:
#        now = time.time()
#        time_to_next_check = (status.lastActive + timeout) - now
#        if time_to_next_check <=0: break
#        time.sleep(time_to_next_check)
#    logger.info("Shutting down due to inactivity")
#    server.shutdown()



if __name__ == '__main__':
    clize.run(main)
