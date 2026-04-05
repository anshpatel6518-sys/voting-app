from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from models import db, Voter, Candidate, Vote
from datetime import datetime
import os
import random
from functools import wraps
import requests
import threading

asedir = os.path.abspath(os.path.dirname(__file__)) 
app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key-123'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(asedir, 'voting.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

ADMIN_PIN = "12345"

# --- GOOGLE SHEETS SETTINGS ---
# Replace this with your deployed Google Apps Script Web App URL
APPS_SCRIPT_URL = 'https://script.google.com/macros/s/AKfycbxk0WCaopqz-5hPHaqowoZ6qbW40OUsKtk9gTzgNimDtAT8QeuUDVz8gYNWJbQHgBRT/exec'

def send_to_google_sheets(voter_name, aadhaar_id, candidate_name, party, timestamp):
    try:
        data = {
            "voter_name": voter_name,
            "aadhaar_id": "xxxx-xxxx-" + aadhaar_id[-4:],  # Masked for privacy
            "candidate_name": candidate_name,
            "party": party,
            "timestamp": timestamp
        }
        # Run HTTP call to Apps script
        res = requests.post(APPS_SCRIPT_URL, json=data, timeout=5)
        print("Google Sheets Sync:", res.text)
    except Exception as e:
        print(f"Error sending to Google Sheets: {e}")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'aadhaar_id' not in session:
            flash("Please login first.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def check_eligibility(voter):
    if voter.age < 18:
        return redirect(url_for('not_eligible'))
    if voter.has_voted:
        return redirect(url_for('already_voted'))
    return None

@app.route('/', methods=['GET'])
def login():
    session.clear()
    return render_template('login.html')

@app.route('/verify-id', methods=['POST'])
def verify_id():
    aadhaar_id = request.form.get('aadhaar_id', '').strip()
    voter = Voter.query.filter_by(aadhaar_id=aadhaar_id).first()
    
    if not voter:
        # Attempt to fetch from Google Sheets Live Database
        try:
            # If the user has deployed the script, this will try to search the new "Voters" tab
            gs_url = f"{APPS_SCRIPT_URL}?action=get_voter&aadhaar_id={aadhaar_id}"
            # Only run if url is actually a real script URL, not placeholder
            if "script.google.com" in APPS_SCRIPT_URL:
                resp = requests.get(gs_url, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success":
                        voter = Voter(
                            aadhaar_id=aadhaar_id,
                            name=data.get("name"),
                            age=int(data.get("age")),
                            mobile_number=str(data.get("mobile_number", "")),
                            fingerprint_id=999 + int(aadhaar_id[-3:]), # Semi-unique finger ID
                            has_voted=False
                        )
                        db.session.add(voter)
                        db.session.commit()
        except Exception as e:
            print(f"Error fetching from Google Sheets: {e}")

    if not voter:
        flash("Aadhaar Number not registered.", "danger")
        return redirect(url_for('login'))
    
    # Check eligibility early
    redirect_resp = check_eligibility(voter)
    if redirect_resp: return redirect_resp
    
    session['temp_aadhaar_id'] = aadhaar_id
    return render_template('verify_voter.html', voter=voter)

@app.route('/confirm-details', methods=['POST'])
def confirm_details():
    if 'temp_aadhaar_id' not in session:
        return redirect(url_for('login'))
    
    aadhaar_id = session['temp_aadhaar_id']
    voter = Voter.query.get(aadhaar_id)
    
    # Final check before moving to OTP
    redirect_resp = check_eligibility(voter)
    if redirect_resp: return redirect_resp
    
    session['aadhaar_id'] = aadhaar_id
    session['name'] = voter.name
    session.pop('temp_aadhaar_id', None)
    
    return redirect(url_for('otp'))

@app.route('/otp', methods=['GET'])
@login_required
def otp():
    generated_otp = str(random.randint(100000, 999999))
    session['otp'] = generated_otp
    return render_template('otp.html', otp=generated_otp)

@app.route('/verify-otp', methods=['POST'])
@login_required
def verify_otp():
    user_otp = request.form.get('otp')
    if 'otp' in session and user_otp == session['otp']:
        session['otp_verified'] = True
        return redirect(url_for('vote'))
    
    flash("Incorrect OTP.", "danger")
    return redirect(url_for('otp'))

@app.route('/vote', methods=['GET'])
@login_required
def vote():
    if not session.get('otp_verified'):
        flash("Please verify OTP first.", "warning")
        return redirect(url_for('otp'))
        
    voter = Voter.query.get(session['aadhaar_id'])
    
    redirect_resp = check_eligibility(voter)
    if redirect_resp: return redirect_resp
        
    candidates = Candidate.query.all()
    return render_template('vote.html', candidates=candidates)

@app.route('/submit-vote', methods=['POST'])
@login_required
def submit_vote():
    if not session.get('otp_verified'):
        flash("Please verify OTP first.", "warning")
        return redirect(url_for('otp'))
        
    voter = Voter.query.get(session['aadhaar_id'])
    redirect_resp = check_eligibility(voter)
    if redirect_resp: return redirect_resp
        
    candidate_id = request.form.get('candidate_id')
    if not candidate_id:
        flash("Please select a candidate.", "danger")
        return redirect(url_for('vote'))
        
    try:
        new_vote = Vote(aadhaar_id=voter.aadhaar_id, candidate_id=candidate_id)
        voter.has_voted = True
        db.session.add(new_vote)
        db.session.commit()
        
        # Local time formatting for display
        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session['vote_timestamp'] = timestamp_str
        
        # Fire and forget Google Sheets sync
        candidate = Candidate.query.get(candidate_id)
        if APPS_SCRIPT_URL != 'https://script.google.com/macros/s/AKfycbxk0WCaopqz-5hPHaqowoZ6qbW40OUsKtk9gTzgNimDtAT8QeuUDVz8gYNWJbQHgBRT/exec':
            threading.Thread(
                target=send_to_google_sheets, 
                args=(voter.name, voter.aadhaar_id, candidate.name, candidate.party, timestamp_str)
            ).start()
            
        return redirect(url_for('confirmation'))
    except Exception as e:
        db.session.rollback()
        flash("An error occurred while casting vote. Please try again.", "danger")
        return redirect(url_for('vote'))

@app.route('/confirmation', methods=['GET'])
@login_required
def confirmation():
    voter = Voter.query.get(session['aadhaar_id'])
    if not voter.has_voted:
        return redirect(url_for('vote'))
    
    timestamp = session.get('vote_timestamp', 'Unknown time')
    name = session.get('name')
    session.clear() 
    return render_template('confirmation.html', name=name, timestamp=timestamp)

@app.route('/already-voted')
def already_voted():
    return render_template('already_voted.html')

@app.route('/not-eligible')
def not_eligible():
    return render_template('not_eligible.html')

@app.route('/admin')
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_results'))
    return render_template('admin_login.html')

@app.route('/admin/login', methods=['POST'])
def do_admin_login():
    pin = request.form.get('pin')
    if pin == ADMIN_PIN:
        session['admin_logged_in'] = True
        return redirect(url_for('admin_results'))
    flash("Invalid Admin PIN.", "danger")
    return redirect(url_for('admin_login'))

@app.route('/admin/results')
def admin_results():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
        
    candidates_data = []
    candidates = Candidate.query.all()
    for c in candidates:
        count = Vote.query.filter_by(candidate_id=c.candidate_id).count()
        candidates_data.append({
            'name': f"{c.name} ({c.party})",
            'votes': count
        })
        
    labels = [c['name'] for c in candidates_data]
    data = [c['votes'] for c in candidates_data]
        
    return render_template('results.html', labels=labels, data=data)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

@app.route('/api/verify-finger')
def verify_finger():
    fingerprint_id = request.args.get('fingerprint_id')
    if not fingerprint_id:
        return jsonify({"verified": False, "error": "fingerprint_id required"}), 400
        
    try:
        finger_id_int = int(fingerprint_id)
        voter = Voter.query.filter_by(fingerprint_id=finger_id_int).first()
        if voter:
            return jsonify({
                "voter_id": voter.aadhaar_id,
                "name": voter.name,
                "verified": True
            })
    except ValueError:
        pass
        
    return jsonify({"verified": False})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='127.0.0.1', port=5001, debug=False)