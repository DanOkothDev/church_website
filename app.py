from flask import Flask, render_template, request, jsonify, current_app, session, redirect, url_for,flash
from collections import deque
from routes import register_blueprints
import os
from dotenv import load_dotenv
import cloudinary
import json
from datetime import datetime
import uuid
import os
import hmac
import hashlib
import base64
import logging
import io
import csv
from flask_mail import Mail, Message
import urllib.request
import urllib.parse

# MPESA / Daraja C2B config placeholders (fill from your Daraja dashboard)
MPESA_ENV = os.getenv('MPESA_ENV', 'sandbox')  # sandbox | production
MPESA_CONSUMER_KEY = os.getenv('MPESA_CONSUMER_KEY')  # put your consumer key here
MPESA_CONSUMER_SECRET = os.getenv('MPESA_CONSUMER_SECRET')  # put your consumer secret here
MPESA_SHORTCODE = os.getenv('MPESA_SHORTCODE')  # e.g. PayBill number
MPESA_C2B_VALIDATION_URL = os.getenv('MPESA_C2B_VALIDATION_URL', os.getenv('BASE_URL', 'https://yourdomain.com') + '/contributions/webhook')
MPESA_C2B_CONFIRMATION_URL = os.getenv('MPESA_C2B_CONFIRMATION_URL', os.getenv('BASE_URL', 'https://yourdomain.com') + '/contributions/webhook')
MPESA_WEBHOOK_SECRET = os.getenv('MPESA_WEBHOOK_SECRET')  # webhook hmac secret

MPESA_BASE_URL = 'https://sandbox.safaricom.co.ke' if MPESA_ENV == 'sandbox' else 'https://api.safaricom.co.ke'
from db import init_app as db_init_app, init_db, add_payment, get_payments, get_summary, set_target_amount
from db import create_campaign, get_campaigns, set_active_campaign, get_active_campaign, get_campaign, get_audits, set_campaign_status
from db import update_payment_status, get_payment_by_checkout_request_id

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "fallback-key")

# Cloudinary setup
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

# Register blueprints from /routes/__init__.py
register_blueprints(app)

# initialize database helper
db_init_app(app)

# Run migrations automatically if environment requests it
if os.getenv('RUN_MIGRATIONS', 'false').lower() in ('1', 'true', 'yes'):
    init_db()

# Ensure logs directory exists and configure webhook logger
os.makedirs('logs', exist_ok=True)
wh_logger = logging.getLogger('webhooks')
wh_logger.setLevel(logging.INFO)
wh_handler = logging.FileHandler('logs/webhooks.log')
wh_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
wh_logger.addHandler(wh_handler)


@app.route('/')
def home():
    # Load events and compute simple upcoming / past summaries for home notifications
    events = []
    try:
        with open('events.json', 'r') as f:
            events = json.load(f)
    except Exception:
        events = []

    from datetime import datetime, date
    today = date.today()
    upcoming = []
    past = []
    for e in events:
        try:
            d = datetime.fromisoformat(e.get('date')).date()
        except Exception:
            continue
        if d >= today:
            upcoming.append((d, e))
        else:
            past.append((d, e))

    upcoming.sort(key=lambda x: x[0])
    past.sort(key=lambda x: x[0], reverse=True)

    events_summary = {
        'upcoming_count': len(upcoming),
        'past_count': len(past),
        'upcoming': [ {'date': d.isoformat(), 'title': ev.get('title','')} for d, ev in upcoming[:3] ],
        'past': [ {'date': d.isoformat(), 'title': ev.get('title','')} for d, ev in past[:3] ],
    }

    return render_template('index.html', events_summary=events_summary)


# Simple calendar route so the calendar page can be reached from the nav
@app.route('/calendar')
def calendar():
    return render_template('calendar.html')


# Contributions page
@app.route('/contributions')
def contributions_page():
    active = get_active_campaign()
    return render_template('contributions.html', active_campaign=active)


@app.route('/contributions/data')
def contributions_data():
    """Return current contributions summary as JSON."""
    # return DB-backed summary; if an active campaign exists, show campaign-specific progress
    active = get_active_campaign()
    if active and active.get('id'):
        campaign_id = active.get('id')
        summary = get_summary(campaign_id=campaign_id)
        payments = get_payments(200)
        # filter payments for campaign on the fly for listing
        payments_filtered = [p for p in payments if p.get('campaign_id') == campaign_id]
        return jsonify({
            'campaign': active,
            'target_amount': summary['target_amount'],
            'total_contributed': summary['total_contributed'],
            'remaining': summary['remaining'],
            'percent': summary['percent'],
            'payments': payments_filtered
        })
    else:
        summary = get_summary()
        payments = get_payments(200)
        return jsonify({
            'target_amount': summary['target_amount'],
            'total_contributed': summary['total_contributed'],
            'remaining': summary['remaining'],
            'percent': summary['percent'],
            'payments': payments
        })


def get_mpesa_oauth_token():
    
    pass


@app.route('/contributions/simulate', methods=['POST'])
def contributions_simulate():
    """Simulate receiving a contribution (for testing). Expects JSON {name, amount}.
    This endpoint is for local testing only and should be disabled or protected in production.
    """
    # Simulation endpoint is disabled by default in production. To enable locally,
    # set the environment variable `ENABLE_MPESA_SIMULATE=true`.
    enabled = os.getenv('ENABLE_MPESA_SIMULATE', 'false').lower() in ('1', 'true', 'yes')
    if not enabled:
        return jsonify({'error': 'simulation endpoint disabled'}), 403

    payload = request.get_json() or {}
    name = payload.get('name', 'Test')
    try:
        amount = float(payload.get('amount', 0) or 0)
    except Exception:
        return jsonify({'error': 'invalid amount'}), 400

    # associate with active campaign if present
    active = get_active_campaign()
    entry = {
        'id': str(uuid.uuid4()),
        'from': name,
        'amount': amount,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'method': 'mpesa-sim',
        'campaign_id': active.get('id') if active else None
    }

    # insert into DB
    try:
        add_payment(entry)
    except Exception as e:
        return jsonify({'error': 'failed to write to DB', 'details': str(e)}), 500
    return jsonify({'status': 'ok', 'entry': entry})


@app.route('/contributions/webhook', methods=['POST'])
def contributions_webhook():
    """Endpoint to receive C2B validation and confirmation callbacks from Mpesa.
    For validation, return success to allow transaction.
    For confirmation, process the payment.
    """
    # Verify webhook signature if configured
    secret = os.getenv('MPESA_WEBHOOK_SECRET')
    raw_body = request.get_data() or b''

    sig_header = (request.headers.get('X-Mpesa-Signature')
                  or request.headers.get('X-Signature')
                  or request.headers.get('X-Callback-Signature'))

    if secret:
        if not sig_header:
            return jsonify({'error': 'missing signature header'}), 401
        try:
            # compute HMAC-SHA256 then base64 encode (common pattern)
            mac = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).digest()
            computed = base64.b64encode(mac).decode()
        except Exception:
            return jsonify({'error': 'signature computation failed'}), 500

        # constant-time comparison
        if not hmac.compare_digest(computed, sig_header):
            return jsonify({'error': 'invalid signature'}), 401
    else:
        # If no secret configured, allow but warn in logs
        current_app.logger.warning('MPESA_WEBHOOK_SECRET not set — accepting webhook without verification')

    # parse payload
    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        payload = {}

    # log raw request body for audit
    try:
        wh_logger.info(request.get_data(as_text=True))
    except Exception:
        current_app.logger.exception('failed to write webhook log')

    # Extract C2B fields
    trans_id = payload.get('TransID')
    amount = payload.get('TransAmount')
    msisdn = payload.get('MSISDN')
    first_name = payload.get('FirstName', '')
    middle_name = payload.get('MiddleName', '')
    last_name = payload.get('LastName', '')
    bill_ref = payload.get('BillRefNumber', '').strip().upper()

    # Map BillRefNumber to category
    categories = {
        'TITHE': 'tithe',
        'OFFERING': 'offering',
        'OFFERINGS': 'offering',
        'BUILDING': 'building_fund',
        'MISSIONS': 'missions',
        'YOUTH': 'youth_ministry',
        'CHILDREN': 'children_ministry',
        'WELFARE': 'welfare',
        'SPECIAL': 'special_offering',
        'THANKSGIVING': 'thanksgiving',
        'SEED': 'seed_offering',
        'FIRSTFRUIT': 'first_fruit',
        'TITHE': 'tithe',
        'TITHES': 'tithe',
        # Add more mappings as needed
    }
    category = categories.get(bill_ref, 'general')

    if not trans_id or not amount or not msisdn:
        return jsonify({'error': 'missing required C2B fields'}), 400

    try:
        amount = float(amount)
    except Exception:
        return jsonify({'error': 'invalid amount'}), 400

    # Check if already processed (idempotency)
    from db import get_payment_by_trans_id
    if get_payment_by_trans_id(trans_id):
        # Already processed, return success
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Already processed'})

    # Build name from fields
    name_parts = [first_name, middle_name, last_name]
    name = ' '.join(p for p in name_parts if p).strip() or msisdn

    # Associate with active campaign only for certain categories
    # Tithes and offerings are regular giving, not campaign-specific
    campaign_categories = ['building_fund', 'missions', 'youth_ministry', 'children_ministry', 'welfare', 'special_offering']
    campaign_id = None
    if category in campaign_categories:
        try:
            active = get_active_campaign()
            if active and active.get('id'):
                campaign_id = active.get('id')
        except Exception:
            pass

    entry = {
        'id': str(uuid.uuid4()),
        'from': name,
        'amount': amount,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'method': 'mpesa-c2b',
        'campaign_id': campaign_id,
        'status': 'completed',
        'trans_id': trans_id,
        'daraja_response': json.dumps(payload),
        'category': category
    }

    # Insert payment
    try:
        add_payment(entry)
    except Exception as e:
        current_app.logger.exception('C2B payment insert failed')
        return jsonify({'error': 'database error'}), 500

    # Return success for validation/confirmation
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})


# Admin: view contributions
@app.route('/admin/contributions')
def admin_contributions():
    if not session.get('admin'):
        # Save desired endpoint so admin login can redirect back
        session['next'] = 'admin_contributions'
        return redirect(url_for('admin.admin_login'))
    summary = get_summary()
    payments = get_payments(500)
    updated = bool(request.args.get('ok'))
    campaigns = get_campaigns()
    audits = []
    try:
        audits = get_audits(200)
    except Exception:
        audits = []

    total_transactions = len(payments)
    unique_donors = len({p.get('from_name', '').strip() for p in payments if p.get('from_name')})
    total_contributed = float(summary.get('total_contributed', 0) if isinstance(summary, dict) else summary.total_contributed)
    average_amount = round(total_contributed / total_transactions, 2) if total_transactions > 0 else 0.0

    # Build category chart data
    category_totals = {}
    for p in payments:
        key = (p.get('category') or 'Uncategorized').strip() or 'Uncategorized'
        try:
            amount = float(p.get('amount', 0))
        except Exception:
            amount = 0.0
        category_totals[key] = category_totals.get(key, 0.0) + amount

    category_labels = list(category_totals.keys())
    category_values = [round(v, 2) for v in category_totals.values()]

    # Compute raised per campaign
    campaign_raised = {}
    for p in payments:
        cid = p.get('campaign_id')
        if cid:
            campaign_raised[cid] = campaign_raised.get(cid, 0) + float(p.get('amount', 0) or 0)

    return render_template(
        'admin_contributions.html',
        payments=payments,
        summary=summary,
        updated=updated,
        campaigns=campaigns,
        audits=audits,
        total_transactions=total_transactions,
        unique_donors=unique_donors,
        average_amount=average_amount,
        category_labels=category_labels,
        category_values=category_values,
        campaign_raised=campaign_raised,
    )


@app.route('/admin/contributions/data')
def admin_contributions_data():
    if not session.get('admin'):
        return jsonify({'error': 'login required'}), 401

    month = request.args.get('month')  # format YYYY-MM or empty
    campaign_id = request.args.get('campaign_id')
    method = request.args.get('method')

    payments = get_payments(10000)

    def match_payment(p):
        if month:
            ts = p.get('timestamp', '')
            if not ts.startswith(month):
                return False
        if campaign_id and campaign_id != 'all':
            if str(p.get('campaign_id', '')) != campaign_id:
                return False
        if method and method != 'all':
            if str(p.get('method', '')).lower() != method.lower():
                return False
        return True

    filtered = [p for p in payments if match_payment(p)]

    total_contributed = sum(float(p.get('amount', 0) or 0) for p in filtered)
    total_transactions = len(filtered)
    unique_donors = len({(p.get('from_name') or '').strip() for p in filtered if p.get('from_name')})
    average_amount = round(total_contributed / total_transactions, 2) if total_transactions else 0.0

    category_totals = {}
    for p in filtered:
        category = (p.get('category') or 'Uncategorized')
        category_totals[category] = category_totals.get(category, 0) + float(p.get('amount', 0) or 0)

    category_labels = list(category_totals.keys())
    category_values = [round(category_totals[k], 2) for k in category_labels]

    # Get target amount: global if no campaign filter, else campaign-specific
    if campaign_id and campaign_id != 'all':
        campaign = get_campaign(campaign_id)
        target_amount = float(campaign.get('target_amount', 0)) if campaign else 0.0
    else:
        global_summary = get_summary()
        target_amount = float(global_summary.get('target_amount', 0) if isinstance(global_summary, dict) else global_summary.target_amount)

    return jsonify({
        'summary': {
            'total_contributed': total_contributed,
            'total_transactions': total_transactions,
            'unique_donors': unique_donors,
            'average_amount': average_amount,
            'target_amount': target_amount,
        },
        'category_labels': category_labels,
        'category_values': category_values,
        'payments': filtered[:200],
    })


# Temporary admin-only debug route to fetch recent server log lines.
# Useful when you can't copy the terminal output; shows tail of `logs/server.log`.
@app.route('/debug/logs')
def debug_logs():
    if not session.get('admin'):
        return redirect(url_for('admin.admin_login'))
    log_path = os.path.join(os.getcwd(), 'logs', 'server.log')
    if not os.path.exists(log_path):
        msg = (
            "No server log found at logs/server.log.\n"
            "Start the app with logging to file, for example:\n\n"
            "python app.py 2>&1 | Tee-Object -FilePath logs/server.log\n\n"
            "Then reproduce the error and refresh this page.\n"
        )
        return current_app.response_class(msg, mimetype='text/plain')

    # read last 500 lines
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            dq = deque(f, maxlen=500)
        text = ''.join(dq)
    except Exception as e:
        text = f'Failed to read log: {e}\n'
    return current_app.response_class(text, mimetype='text/plain')


@app.route('/admin/campaigns/create', methods=['POST'])
def create_campaign_route():
    if not session.get('admin'):
        session['next'] = 'create_campaign_route'
        return redirect(url_for('admin.admin_login'))
    title = request.form.get('title') or (request.json and request.json.get('title'))
    target = request.form.get('target_amount') or (request.json and request.json.get('target_amount'))
    if not title or not target:
        return redirect(url_for('admin_contributions'))
    try:
        target_f = float(target)
    except Exception:
        target_f = 0.0
    entry = {
        'id': str(uuid.uuid4()),
        'title': title,
        'description': '',
        'target_amount': target_f,
        'created_at': datetime.utcnow().isoformat() + 'Z',
        'active': 0
    }
    try:
        # call DB helper directly (avoid name collision)
        create_campaign(entry)
    except Exception:
        current_app.logger.exception('failed create campaign')
    return redirect(url_for('admin_contributions'))


@app.route('/admin/campaigns/<campaign_id>/activate', methods=['POST'])
def activate_campaign(campaign_id):
    if not session.get('admin'):
        session['next'] = 'activate_campaign'
        return redirect(url_for('admin.admin_login'))
    try:
        set_active_campaign(campaign_id)
    except Exception:
        current_app.logger.exception('failed to activate')
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'action': 'activate'})
    return redirect(url_for('admin_contributions'))


@app.route('/admin/campaigns/<campaign_id>/status', methods=['POST'])
def update_campaign_status(campaign_id):
    if not session.get('admin'):
        session['next'] = 'update_campaign_status'
        return redirect(url_for('admin.admin_login'))
    new_status = request.form.get('status') or (request.json and request.json.get('status'))
    if not new_status:
        return redirect(url_for('admin_contributions'))
    try:
        set_campaign_status(campaign_id, new_status)
    except Exception:
        current_app.logger.exception('failed to set campaign status')
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'action': 'status', 'new_status': new_status})
    return redirect(url_for('admin_contributions'))


@app.route('/admin/contributions/export')
def export_contributions():
    if not session.get('admin'):
        session['next'] = 'export_contributions'
        return redirect(url_for('admin.admin_login'))
    payments = get_payments(10000)
    # build CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['id', 'timestamp', 'from', 'amount', 'method', 'campaign_id', 'campaign_title'])
    for p in payments:
        cid = p.get('campaign_id')
        title = ''
        try:
            if cid:
                c = get_campaign(cid)
                title = c.get('title') if c else ''
        except Exception:
            title = ''
        writer.writerow([p.get('id'), p.get('timestamp'), p.get('from_name'), p.get('amount'), p.get('method'), cid, title])
    resp = current_app.response_class(output.getvalue(), mimetype='text/csv')
    resp.headers.set('Content-Disposition', 'attachment', filename='contributions.csv')
    return resp


@app.route('/admin/contributions/target', methods=['POST'])
def admin_set_target():
    if not session.get('admin'):
        session['next'] = 'admin_set_target'
        return redirect(url_for('admin.admin_login'))
    # accept form or JSON
    amt = None
    if request.form and 'target_amount' in request.form:
        amt = request.form.get('target_amount')
    else:
        data = request.get_json(silent=True) or {}
        amt = data.get('target_amount')

    try:
        amt_f = float(amt)
    except Exception:
        return jsonify({'error': 'invalid amount'}), 400

    try:
        set_target_amount(amt_f)
    except Exception as e:
        current_app.logger.exception('failed to set target')
        return jsonify({'error': 'failed to update target', 'details': str(e)}), 500

    return '', 302, {'Location': (request.referrer or '/admin/contributions') + '?ok=1'}


app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'omondiokothdan@gmail.com'
app.config['MAIL_PASSWORD'] = 'pukq qnqk yeag dkbr'

mail = Mail(app)

@app.route('/feedback', methods=['GET', 'POST'])
def feedback():
    name = request.form.get('name')
    email = request.form.get('email')
    message = request.form.get('message')

    print(f"{name} | {email} | {message}")

    try:
        msg = Message(
            subject="We received your message 🙏",
            sender=app.config['MAIL_USERNAME'],
            recipients=[email]
        )

        msg.body = f"""
Hello {name},

Thank you for contacting Rapogi Lwanda SDA Church.

We received your message:
"{message}"

We will respond shortly.

Blessings 🙏
        """

        mail.send(msg)
        flash("Feedback sent successfully!", "success")

    except Exception as e:
        print("Email failed:", e)
        flash("Message received, but email reply failed.", "warning")

    return redirect(url_for('home'))

# Error handlers (optional but pro)
@app.errorhandler(404)
def not_found(_):
    return render_template('404.html'), 404


@app.errorhandler(500)
def server_error(_):
    return render_template('500.html'), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
