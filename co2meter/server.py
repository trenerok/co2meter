""" Flask server for CO2meter

    (c) Vladimir Filimonov, 2018
    E-mail: vladimir.a.filimonov@gmail.com
    Redacted by Pol Smith, 2019
    @trenerok
"""
import optparse
import logging
import threading
import time
import glob
import os
import socket
import datetime as dt
import telebot
from telebot import types
import signal
import json

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

import flask
from flask import request, render_template, jsonify
import pandas as pd

import co2meter as co2

_DEFAULT_HOST = '127.0.0.1'
_DEFAULT_PORT = '1201'
_DEFAULT_INTERVAL = 30  # seconds
_DEFAULT_NAME = 'co2'
_INIT_TIME = 30  # time to initialize and calibrate device
_URL = 'https://github.com/vfilimonov/co2meter'
_URL_IMAGES = 'https://user-images.githubusercontent.com/'

_COLORS = {'r': '#E81F2E', 'y': '#FAAF4C', 'g': '#7FB03F'}
_COLORS_HUMAN = {'r': 'red', 'y': 'yellow', 'g': 'green'}

_IMG_G = '1324881/36358454-d707e2f4-150e-11e8-9bd1-b479e232f28f'
_IMG_Y = '1324881/36358456-d8b513ba-150e-11e8-91eb-ade37733b19e'
_IMG_R = '18612132/73597400-9e098600-453c-11ea-8d97-31c1ad7d5fe3'
_RANGE_MID = [800, 1200]
_CO2_MAX_VALUE = 3200  # Cut our yaxis here
##
_SEND_TELEGRAM = True
_TELEGRAM_BOT_KEY = "telegram bot token here"
_ALERT_TIME_GAP_SECOND = 360

_name = _DEFAULT_NAME

###############################################################################
mon = None

###############################################################################
app = flask.Flask(__name__)
app.jinja_env.auto_reload = True
app.config['TEMPLATES_AUTO_RELOAD'] = True


###############################################################################
@app.route('/')
def home():
    # Read CO2 and temp values
    if mon is None:
        status = '<h1 align="center" style="color:%s;">Device is not connected</h1>' % _COLORS['r']
    else:
        status = ''
    try:
        vals = list(mon._last_data)
        vals[-1] = '%.1f' % vals[-1]
    except:
        data = read_logs()
        vals = data.split('\n')[-2].split(',')
        if status == '':
            status = '<h1 align="center" style="color:%s;">Device is not ready</h1>' % _COLORS['r']
    # Select image and color
    if int(vals[1]) >= _RANGE_MID[1]:
        color = _COLORS['r']
        img = _IMG_R
    elif int(vals[1]) < _RANGE_MID[0]:
        color = _COLORS['g']
        img = _IMG_G
    else:
        color = _COLORS['y']
        img = _IMG_Y
    co2 = '<font color="%s">%s ppm</font>' % (color, vals[1])
    # Return template
    return render_template('index.html', image=img, timestamp=vals[0],
                           co2=vals[1], color=color, temp=vals[2], url=_URL,
                           status=status)


#############################################################################
@app.route('/log', defaults={'logname': None})
@app.route('/log/<string:logname>')
def log(logname):
    data = read_logs(name=logname)
    return '<h1>Full log</h1>' + wrap_table(data)


@app.route('/log.csv', defaults={'logname': None})
@app.route('/log/<string:logname>.csv')
def log_csv(logname):
    data = read_logs(name=logname)
    return wrap_csv(data, logname)


@app.route('/log.json', defaults={'logname': None})
@app.route('/log/<string:logname>.json')
def log_json(logname):
    data = read_logs(name=logname)
    return wrap_json(data)


#############################################################################
@app.route('/rename')
def get_shape_positions():
    args = request.args
    logging.info('rename', args.to_dict())
    new_name = args.get('name', default=None, type=str)
    if new_name is None:
        return 'Error: new log name is not specified!'
    global _name
    _name = new_name
    return 'Log name has changed to "%s"' % _name


#############################################################################
@app.route('/kill')
def shutdown():
    server_stop()
    global _monitoring
    _monitoring = False
    return 'Server shutting down...'


#############################################################################
# Dashboard on plotly.js
#############################################################################
def prepare_data(name=None, span='24H'):
    data = read_logs(name)
    data = pd.read_csv(StringIO(data), parse_dates=[0]).set_index('timestamp')
    if span != 'FULL':
        data = data.last(span)

    if span == '24H':
        data = data.resample('60s').mean()
    elif span == '7D':
        data = data.resample('600s').mean()
    elif span == '30D':
        data = data.resample('1H').mean()
    elif span == 'FULL':
        if len(data) > 3000:  # Resample only long series
            data = data.resample('1H').mean()
    data = data.round({'co2': 0, 'temp': 1})
    return data


def rect(y0, y1, color):
    return {'type': 'rect', 'layer': 'below',
            'xref': 'paper', 'x0': 0, 'x1': 1,
            'yref': 'y', 'y0': y0, 'y1': y1,
            'fillcolor': color, 'opacity': 0.2, 'line': {'width': 0}}


def caption(title, x, y):
    return {'xref': 'paper', 'yref': 'paper', 'x': x, 'y': y, 'text': title,
            'showarrow': False, 'font': {'size': 16},
            'xanchor': 'center', 'yanchor': 'bottom'}


#############################################################################
@app.route("/chart/", strict_slashes=False)
@app.route("/chart/<name>", strict_slashes=False)
@app.route("/chart/<name>/<freq>", strict_slashes=False)
def chart_co2_temp(name=None, freq='24H'):
    data = prepare_data(name, freq)

    co2_min = min(500, data['co2'].min() - 50)
    co2_max = min(max(2000, data['co2'].max() + 50), _CO2_MAX_VALUE)
    t_min = min(15, data['temp'].min())
    t_max = max(27, data['temp'].max())

    rect_green = rect(co2_min, _RANGE_MID[0], _COLORS['g'])
    rect_yellow = rect(_RANGE_MID[0], _RANGE_MID[1], _COLORS['y'])
    rect_red = rect(_RANGE_MID[1], co2_max, _COLORS['r'])

    # Check if mobile
    try:
        agent = request.headers.get('User-Agent')
        phones = ['iphone', 'android', 'blackberry', 'fennec', 'iemobile']
        staticPlot = any(phone in agent.lower() for phone in phones)
    except RuntimeError:
        staticPlot = False

    # Make figure
    index = data.index.format()
    co2 = list(pd.np.where(data.co2.isnull(), None, data.co2))
    temp = list(pd.np.where(data.temp.isnull(), None, data.temp))

    d_co2 = {'mode': 'lines+markers', 'type': 'scatter',
             'name': 'CO2 concentration',
             'xaxis': 'x1', 'yaxis': 'y1',
             'x': index, 'y': co2}
    d_temp = {'mode': 'lines+markers', 'type': 'scatter',
              'name': 'Temperature',
              'xaxis': 'x1', 'yaxis': 'y2',
              'x': index, 'y': temp}

    config = {'displayModeBar': False, 'staticPlot': staticPlot}
    layout = {'margin': {'l': 30, 'r': 10, 'b': 30, 't': 30},
              'showlegend': False,
              'shapes': [rect_green, rect_yellow, rect_red],
              'xaxis1': {'domain': [0, 1], 'anchor': 'y2'},
              'yaxis1': {'domain': [0.55, 1], 'anchor': 'free', 'position': 0,
                         'range': [co2_min, co2_max]},
              'yaxis2': {'domain': [0, 0.45], 'anchor': 'x1',
                         'range': [t_min, t_max]},
              'annotations': [caption('CO2 concentration', 0.5, 1),
                              caption('Temperature', 0.5, 0.45)]
              }
    fig = {'data': [d_co2, d_temp], 'layout': layout, 'config': config}
    return jsonify(fig)


#############################################################################
@app.route("/dashboard")
def dashboard_plotly():
    # Get list of files
    files = glob.glob('logs/*.csv')
    files = [os.path.splitext(os.path.basename(_))[0] for _ in files]
    # And find selected for jinja template
    files = [(_, _ == _name) for _ in files]
    return render_template('dashboard.html', files=files)


#############################################################################
# Monitoring routines
#############################################################################


def now():
    return dt.datetime.now().replace(microsecond=0)


def opendatetime():
    try:
        f = open('logs/' + 'lastdatetime.log', 'r')
        data = f.readline()
        result = json.loads(data)
        f.close()
        return result
    except Exception as err:
        logging.info('Something goes wrong [%s]' % str(err))


def savedatetime(last_ivent):
    try:
        f = open('logs/' + 'lastdatetime.log', 'w')
        nowdatetime = str(dt.datetime.utcnow().timestamp())
        result = {"datetime": nowdatetime, "last_event": last_ivent}
        f.write(json.dumps(result) + "\n")
        f.close()
        return result
    except Exception as err:
        logging.info('Something goes wrong [%s]' % str(err))

def send_telegram(data):
    bot = telebot.TeleBot(_TELEGRAM_BOT_KEY)
    keyboard = types.InlineKeyboardMarkup()

    try:
        savedatetime(data.get('color'))
        bot.send_photo(-280116721, photo=data.get('img'),
                       parse_mode='HTML',
                       caption="<b>CO2 Monitor</b>" + '\n' + str(data.get('data')) + '\n' + '\n' \
                            "<b>timestamp: </b>" + str(dt.datetime.now().__format__('%Y-%m-%dT%H:%M:%S')) + '\n' + '\n', reply_markup=keyboard)
        logging.info('telegram send DONE')
    except Exception as err:
        logging.info('- Error send events  to Telegram, error - [%s]' % str(err))


def read_logs(name=None):
    """ read log files """
    if name is None:
        name = _name
    with open(os.path.join('logs', name + '.csv'), 'r') as f:
        data = f.read()
    return data


#############################################################################
def write_to_log(vals):
    """ file name for a current log """
    # Create file if does not exist
    fname = os.path.join('logs', _name + '.csv')
    if not os.path.exists('logs'):
        os.makedirs('logs')
    if not os.path.isfile(fname):
        with open(fname, 'a') as f:
            f.write('timestamp,co2,temp\n')
    # Append to file
    with open(fname, 'a') as f:
        f.write('%s,%d,%.1f\n' % vals)


def read_co2_data():
    """ A small hack to read co2 data from monitor in order to account for case
        when monitor is not initialized yet
    """
    global mon
    if mon is None:
        # Try to initialize
        try:
            mon = co2.CO2monitor()
            # Sleep. If we read from device before it is calibrated, we'll
            # get wrong values
            time.sleep(_INIT_TIME)
        except OSError:
            return None
    try:
        return mon.read_data_raw(max_requests=1000)
    except OSError:
        # We kill the link and will require to initialize monitor again next time
        mon = None
        return None


def monitoring_CO2(interval):
    """ Tread for monitoring / logging """

    def send_notify(result):
        possible = True
        if os.path.isfile('logs/' + 'lastdatetime.log'):
            current_datetime = dt.datetime.utcnow().timestamp()
            last_datetime = opendatetime().get('datetime') ####need check
            timedelta = float(current_datetime) - float(last_datetime)
            #print('Timedalta in seconds: ' + str(timedelta))
            if timedelta < float(_ALERT_TIME_GAP_SECOND):
                #print("It's not time yet.. Spend less than %s seconds.." % (
                    #str(_ALERT_TIME_GAP_SECOND)))
                possible = False

        result['data'] = "Current CO2 level: <b>" + str(result.get('co2')) + "</b> PPM" + "\n" \
        "Temperature: <b>" + str(result.get('temp')) + "</b> degree"

        return send_telegram(result) if possible is True else None

    while _monitoring:
        # Request concentration and temperature
        vals = read_co2_data()
        if vals is None:
            logging.info('[%s] monitor is not connected' % co2.now())
        else:
            # Write to log and sleep
            logging.info('[%s] %d ppm, %.1f deg C' % tuple(vals))
            write_to_log(vals)

            #####telegram handler
            if _SEND_TELEGRAM:
                result = {'co2': vals[1], 'temp': format(vals[2], '.1f'), 'timestamp': now()}

                ## Select image and color for telegram
                if int(vals[1]) >= _RANGE_MID[1]:
                    color = _COLORS_HUMAN['r']
                    img = _IMG_R
                    result['img'] = _URL_IMAGES + img + '.jpg'
                    result['color'] = color
                    send_notify(result)

                elif int(vals[1]) < _RANGE_MID[0]:
                    color = _COLORS_HUMAN['g']
                    img = _IMG_G
                    result['img'] = _URL_IMAGES + img + '.jpg'
                    result['color'] = color
                    ##send notify only if last event was red
                    if os.path.isfile('logs/' + 'lastdatetime.log'):
                        last_event = opendatetime().get('last_event')
                        if last_event == 'red':
                            send_notify(result)

                else:
                    color = _COLORS_HUMAN['y']
                    img = _IMG_Y
                    result['img'] = _URL_IMAGES + img + '.jpg'
                    result['color'] = color
                    ##test for yellow delete this
                    #send_notify(result)

        # Sleep for the next call
        time.sleep(interval)


#############################################################################
def start_monitor(interval=_DEFAULT_INTERVAL):
    """ Start CO2 monitoring in a thread """
    logging.basicConfig(level=logging.INFO)

    global _monitoring
    _monitoring = True
    t = threading.Thread(target=monitoring_CO2, args=(interval,))
    t.start()
    return t


#############################################################################
def init_homekit_target(port, host):
    try:
        from .homekit import start_homekit
    except:
        from homekit import start_homekit

    global mon
    while mon is None:
        time.sleep(5)
    logging.info('Starting homekit server')
    start_homekit(mon, host=host, port=port, monitoring=False, handle_sigint=False)


def init_homekit(port, host):
    # We'll start homekit once the device is connected
    t = threading.Thread(target=init_homekit_target, args=(port, host, ))
    t.start()


#############################################################################
# Server routines
#############################################################################
def my_ip():
    """ Get my local IP address """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))  # Google Public DNS
        return s.getsockname()[0]


def start_server_homekit():
    """ Start monitoring, flask/dash server and homekit accessory """
    # Based on http://flask.pocoo.org/snippets/133/
    try:
        from .homekit import PORT
    except:
        # the case of running not from the installed module
        from homekit import PORT

    host = my_ip()
    parser = optparse.OptionParser()
    parser.add_option("-H", "--host",
                      help="Hostname of the Flask app [default %s]" % host,
                      default=host)
    parser.add_option("-P", "--port-flask",
                      help="Port for the Flask app [default %s]" % _DEFAULT_PORT,
                      default=_DEFAULT_PORT)
    parser.add_option("-K", "--port-homekit",
                      help="Port for the Homekit accessory [default %s]" % PORT,
                      default=PORT)
    parser.add_option("-N", "--name",
                      help="Name for the log file [default %s]" % _DEFAULT_NAME,
                      default=_DEFAULT_NAME)
    options, _ = parser.parse_args()

    global _name
    _name = options.name

    # Start monitoring
    t_monitor = start_monitor()
    # Start a thread that will initialize homekit once device is connected
    init_homekit(host=options.host, port=int(options.port_homekit))
    # Start server
    app.run(host=options.host, port=int(options.port_flask))


#############################################################################
def start_server():
    """ Runs Flask instance using command line arguments """
    # Based on http://flask.pocoo.org/snippets/133/
    parser = optparse.OptionParser()
    parser.add_option("-H", "--host",
                      help="Hostname of the Flask app [default %s]" % _DEFAULT_HOST,
                      default=_DEFAULT_HOST)
    parser.add_option("-P", "--port",
                      help="Port for the Flask app [default %s]" % _DEFAULT_PORT,
                      default=_DEFAULT_PORT)
    parser.add_option("-I", "--interval",
                      help="Interval in seconds for CO2meter requests [default %d]" % _DEFAULT_INTERVAL,
                      default=_DEFAULT_INTERVAL)
    parser.add_option("-N", "--name",
                      help="Name for the log file [default %s]" % _DEFAULT_NAME,
                      default=_DEFAULT_NAME)
    parser.add_option("-m", "--nomonitoring",
                      help="No live monitoring (only flask server)",
                      action="store_true", dest="no_monitoring")
    parser.add_option("-s", "--noserver",
                      help="No server (only monitoring to file)",
                      action="store_true", dest="no_server")
    parser.add_option("-d", "--debug",
                      action="store_true", dest="debug",
                      help=optparse.SUPPRESS_HELP)
    options, _ = parser.parse_args()

    if options.debug and not options.no_monitoring:
        parser.error("--debug option could be used only with --no_monitoring")
    global _name
    _name = options.name

    # Start monitoring
    if not options.no_monitoring:
        start_monitor(interval=int(options.interval))

    # Start server
    if not options.no_server:
        app.run(debug=options.debug, host=options.host, port=int(options.port))


def stop_server():
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        raise RuntimeError('Not running with the Werkzeug Server')
    func()


###############################################################################
def wrap_csv(data, fname='output'):
    """ Make CSV response downloadable """
    if fname is None:
        fname = 'log'
    si = StringIO(data)
    output = flask.make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=%s.csv" % fname
    output.headers["Content-type"] = "text/csv"
    return output


def wrap_json(data):
    """ Convert CSV to JSON and make it downloadable """
    entries = [_.split(',') for _ in data.split('\n') if _ != '']
    js = [{k: v for k, v in zip(['timestamp', 'co2', 'temp'], x)}
          for x in entries[1:]]
    return jsonify(js)


def wrap_table(data):
    """ Return HTML for table """
    res = ('<table><thead><tr><th>Timestamp</th><th>CO2 concentration</th>'
           '<th>Temperature</th></tr></thead><tbody>')
    for line in data.split('\n')[1:]:
        res += '<tr>' + ''.join(['<td>%s</td>' % d for d in line.split(',')]) + '</tr>'
    res += '</tbody></table>'
    return res


###############################################################################
if __name__ == '__main__':
    # start_server() will take care of start_monitor()
    start_server()
    # start_server_homekit()
