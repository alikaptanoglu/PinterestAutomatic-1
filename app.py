import os
from flask import Flask, render_template, session, request, redirect, url_for, flash, jsonify, abort
from flask_babelex import Babel
from flask_sqlalchemy import SQLAlchemy
from flask_user import current_user, login_required
from config import ConfigClass
from rq import Queue
import requests
from rq.job import Job
from worker import conn
import urllib.parse as urlparse
import time

app = Flask(__name__)
app.config.from_object(ConfigClass)

q = Queue(connection=conn, default_timeout=1200)

db = SQLAlchemy(app)
babel = Babel(app)
from services import get_token, save_token_to_database, get_next_pins, save_profile_and_return_requests_left, get_last_pin_details, update_pin_data, update_stats, save_ip
from models import User

# Create all database tables and create admin
# db.create_all()
# create_admin_if_not_exists()

PINTEREST_CLIENT_ID = os.environ.get("PINTEREST_CLIENT_ID", default="4844984301336407368")
PINTEREST_API_BASE_URL = os.environ.get("PINTEREST_API_BASE_URL")
SITE_SCHEME = os.environ.get("SITE_SCHEME", default="https")
SITE_DOMAIN = os.environ.get("SITE_DOMAIN", default="pinautomatic.herokuapp.com")


@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    else:
        return redirect('/user/sign-in')


@app.route('/privacy-policy')
def privacy_policy():
    return render_template('privacy_policy.html')


@app.route('/home')
@login_required
def home():
    credentials = {
        'client_id': PINTEREST_CLIENT_ID,
        'redirect_uri': SITE_SCHEME + "://" + SITE_DOMAIN
    }

    if 'pa-token' in session:
        return render_template('index.html', title='Home')
    else:
        return render_template('authorize.html', title='Login', credentials=credentials)


@app.route('/pinterest-auth')
@login_required
def pinterest_auth():
    if request.args.get('state', None) == "secret":
        if request.args.get('code', None):
            temp_code = request.args.get('code', None)
            access_token = get_token(temp_code)
            save_token_to_database(access_token)

            session['pa-token'] = access_token

            return redirect(url_for('index'))
        else:
            flash("You need to provide authorization to PinAutomatic to allow adding of pins to your board.", category='error')
            return redirect(url_for('home'))
    else:
        return redirect('/user/sign-out')


@app.route('/pin-it')
@login_required
def pin_it():
    if not check_user_active():
        return jsonify({"code": 401, "data": "/user/sign-out"})
    parsed = urlparse.urlparse(request.url)
    source = urlparse.parse_qs(parsed.query)['source'][0]
    destination = urlparse.parse_qs(parsed.query)['destination'][0]
    requests_left = urlparse.parse_qs(parsed.query)['requests_left'][0]
    cont = urlparse.parse_qs(parsed.query)['cont'][0]
    cursor = urlparse.parse_qs(parsed.query)['cursor'][0]
    pa_token = session['pa-token']

    try:
        r = get_next_pins(source, requests_left, cont, cursor)
        all_pins = r["all_pins"]
        last_cursor = r["last_cursor"]
    except:
        abort(400)

    try:
        job = q.enqueue_call(
            func=save_pins, args=(all_pins, source, destination, last_cursor, pa_token, current_user.id), result_ttl=1200
        )
        session['job_id'] = job.get_id()
    except:
        abort(400)

    response = {
        "data": "Pins(s) will be added.",
        "code": 200
    }
    return jsonify(response)


@app.route('/get-requests-left')
@login_required
def get_requests_left():
    res = save_profile_and_return_requests_left()
    time.sleep(2)
    if res['code'] == 200:
        requests_left = res['data']
    elif res['code'] == 401:
        session.pop('pa-token')
        return {'code': 401, 'data': '/user/sign-out'}
    session['req_left'] = requests_left
    save_ip()
    return {'code': 200, 'data': requests_left}


@app.route('/check-last-pin-status')
@login_required
def check_last_pin_status():
    parsed = urlparse.urlparse(request.url)
    source = urlparse.parse_qs(parsed.query)['source'][0]
    destination = urlparse.parse_qs(parsed.query)['destination'][0]
    last_pin_details = get_last_pin_details(source, destination)
    res = {
        "code": 404
    }

    if last_pin_details:
        res = {
            "code": 200,
            "pins_copied": last_pin_details['pins_copied'],
            "cursor": last_pin_details['cursor']
        }

    return res


@app.route('/check-session-status')
@login_required
def check_session_status():
    session_status = dict()
    if "job_id" in session:
        job_key = session['job_id']
        try:
            job = Job.fetch(job_key, connection=conn)
        except Exception:
            session_status = {
                "status": "No pending Job.",
                "code": 404
            }
            return session_status

        if job.is_finished:
            session_status["status"] = "Last Job completed."
            session_status["code"] = 200
        elif job.is_failed:
            session_status["status"] = "Last Job failed. Please enter new one."
            session_status["code"] = 500
        elif not job.is_finished:
            session_status["status"] = "Not yet completed. Pinning..."
            session_status["code"] = 202
    else:
        session_status = {
            "status": "No pending Job.",
            "code": 404
        }

    return session_status


# # The Admin page requires an 'Admin' role.
# @app.route('/admin')
# @roles_required('Admin')    # Use of @roles_required decorator
# def admin_page():
#     pass


def save_pins(pins, source, destination, last_cursor, pa_token, current_user_id):
    counter = 0

    for pin in pins:
        url = PINTEREST_API_BASE_URL + '/pins/?access_token=' + pa_token + "&fields=id"

        post_data = {
            "board": destination,
            "note": str(pin["note"]),
            # "link": "https://pinautomatic.herokuapp.com",
            # Adding links is not feasible as these are
            # Pinterest Links and Pinterest API doesn't
            # allow adding them. Till then adding own link.
            "image_url": pin["image"]["original"]["url"]
        }

        r = requests.post(url, data=post_data)

        if r.status_code == 201:
            counter = counter + 1

            if counter == 100:
                update_stats(counter, current_user_id)
                update_pin_data(source, destination, counter, last_cursor, current_user_id)
                counter = 0

    del pins

    update_stats(counter, current_user_id)
    update_pin_data(source, destination, counter, last_cursor, current_user_id)

    res = {
        "last_cursor": last_cursor,
        "pins_added": counter
    }

    return {"code": 200, "data": res}


@app.route('/toggle-user-active/<user_id>')
def toggle_user_active(user_id):
    user_instance = User.query.filter_by(id=user_id).first()
    if user_instance.active:
        user_instance.active = False
        flag = "INACTIVE"
    else:
        user_instance.active = True
        flag = "ACTIVE"

    db.session.commit()
    return jsonify({"code": 200, "message": str(user_instance.email) + " has been marked active " + str(flag)})


def check_user_active():
    user_instance = User.query.filter_by(id=current_user.id).first()
    if user_instance.active:
        return True
    else:
        return False
