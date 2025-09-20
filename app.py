from flask import Flask, request, jsonify, render_template, send_file, Response
from flask_cors import CORS
from flask_mail import Mail, Message
import google.generativeai as genai
import os
from dotenv import load_dotenv
import logging
import csv
import io
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from io import StringIO
import smtplib
from premailer import transform
import datetime
import json
from flask_socketio import SocketIO, emit


# Load environment variables
load_dotenv()

# Flask app init
app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# SMTP Config - using direct env variables instead of app.config
SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USERNAME = os.getenv('SMTP_USERNAME')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')
MAIL_DEFAULT_SENDER = os.getenv('MAIL_DEFAULT_SENDER', SMTP_USERNAME)

# Configure Flask-Mail with direct init args
mail = Mail()
app.extensions['mail'] = mail

# Logging setup
logging.basicConfig(level=logging.INFO)

# Gemini AI config
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
genai.configure(api_key=GEMINI_API_KEY)

# Create directories for logs and CSV files
os.makedirs('logs', exist_ok=True)
os.makedirs('csv_files', exist_ok=True)

def log_email_attempt(email, name, subject, status, error=None):
    """Log email attempt to both CSV and text files"""
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # CSV logging
    csv_filename = f"csv_files/email_logs_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
    file_exists = os.path.exists(csv_filename)
    
    with open(csv_filename, 'a', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['timestamp', 'name', 'email', 'subject', 'status', 'error']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        
        if not file_exists:
            writer.writeheader()
        
        writer.writerow({
            'timestamp': timestamp,
            'name': name or 'N/A',
            'email': email,
            'subject': subject,
            'status': status,
            'error': error or ''
        })
    
    # Text logging
    log_filename = f"logs/{status}_emails_{datetime.datetime.now().strftime('%Y%m%d')}.txt"
    with open(log_filename, 'a', encoding='utf-8') as logfile:
        log_entry = f"[{timestamp}] {status.upper()}: {email} ({name or 'N/A'}) - Subject: {subject}"
        if error:
            log_entry += f" - Error: {error}"
        logfile.write(log_entry + '\n')
    
    # Emit real-time log to frontend
    socketio.emit('email_log', {
        'timestamp': timestamp,
        'name': name or 'N/A',
        'email': email,
        'subject': subject,
        'status': status,
        'error': error
    })

@app.route('/')
def serve_frontend():
    return render_template('index.html')

@app.route('/generate-email', methods=['POST'])
def generate_email():
    try:
        data = request.get_json()
        prompt = data.get('prompt', '')

        if not prompt:
            return jsonify({'error': 'No prompt provided'}), 400

        app.logger.info(f"Generating email for prompt: {prompt}")

        generation_config = {
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 40,
            "max_output_tokens": 2048,
        }

        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        ]

        model = genai.GenerativeModel(
            model_name="gemini-1.5-pro",
            generation_config=generation_config,
            safety_settings=safety_settings
        )

        full_prompt = f"""
        You are an expert email copywriter specializing in educational technology promotions. 
        Create compelling, professional email content that converts. 
        Always respond with properly formatted HTML email content.

        Create a promotional email for educational technology based on this prompt: {prompt}.

        The email should include:
        1. A subject line
        2. Preheader text
        3. Full HTML body with inline CSS suitable for email clients
        4. Professional design with clear call-to-action

        Make sure the email is responsive and looks good on both desktop and mobile devices.
        """

        response = model.generate_content(full_prompt)
        email_content = response.text

        if not email_content.strip():
            return jsonify({'error': 'Failed to generate email content'}), 500

        subject_line = email_content.splitlines()[0]
        subject = subject_line.split(":", 1)[1].strip() if "subject:" in subject_line.lower() else "Registration for Workshop Infy Skill Edutech"

        return jsonify({'email': email_content, 'subject': subject})

    except Exception as e:
        app.logger.error(f"Error generating email: {e}")
        if "API_KEY_INVALID" in str(e):
            return jsonify({'error': 'Invalid Gemini API key'}), 401
        elif "quota" in str(e).lower():
            return jsonify({'error': 'Gemini API quota exceeded'}), 429
        else:
            return jsonify({'error': 'Internal server error'}), 500

@app.route('/send-email', methods=['POST'])
def send_email_route():
    try:
        data = request.get_json()
        recipient = data.get('recipient')
        subject = data.get('subject', 'Registration for Workshop Infy Skill Edutech')
        body = data.get('body')
        recipient_name = data.get('name', '')

        if not all([recipient, subject, body]):
            return jsonify({'error': 'Missing recipient, subject, or body'}), 400

        msg = MIMEMultipart()
        msg['From'] = SMTP_USERNAME
        msg['To'] = recipient
        msg['Subject'] = subject  # Use dynamic subject
        msg.attach(MIMEText(body, 'html'))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)

        log_email_attempt(recipient, recipient_name, subject, 'success')
        app.logger.info(f"Email sent to {recipient}")
        return jsonify({'message': f'Email sent successfully to {recipient}'}), 200

    except Exception as e:
        log_email_attempt(recipient, recipient_name, subject, 'failure', str(e))
        app.logger.error(f"Error sending email to {recipient}: {e}")
        return jsonify({'error': 'Failed to send email. Check credentials or network.'}), 500

@app.route('/bulk-send', methods=['POST'])
def bulk_send_route():
    try:
        email_data = request.form.get('email_data')
        subject = request.form.get('subject', 'EduTech Promotion')

        if not email_data:
            return jsonify({'error': 'Missing "email_data" in form-data. This is the HTML body of the email.'}), 400

        if 'csv_file' not in request.files:
            return jsonify({'error': 'CSV file is required'}), 400


        if 'csv_file' not in request.files:
            return jsonify({'error': 'CSV file is required'}), 400

        csv_file = request.files['csv_file']
        if not csv_file.filename.lower().endswith('.csv'):
            return jsonify({'error': 'Uploaded file must be a CSV'}), 400

        try:
            csv_content = csv_file.read().decode('utf-8')
        except Exception as e:
            return jsonify({'error': 'Could not decode CSV file. Please use UTF-8 encoding.'}), 400

        csv_reader = csv.DictReader(StringIO(csv_content))
        recipients = list(csv_reader)
        if not recipients:
            return jsonify({'error': 'No recipients found in CSV. Ensure the file has a header row and at least one data row.'}), 400

        email_col = None
        for col in csv_reader.fieldnames:
            if col.strip().lower() == 'email':
                email_col = col
                break
        if not email_col:
            return jsonify({'error': 'CSV must have an "email" column in the header.'}), 400

        attachments = []
        if 'attachments' in request.files:
            files = request.files.getlist('attachments')
            for file in files:
                if file.filename:
                    file_path = os.path.join('uploads', file.filename)
                    os.makedirs('uploads', exist_ok=True)
                    file.save(file_path)
                    attachments.append(file_path)

        sent = 0
        failed = []

        for idx, row in enumerate(recipients):
            to_email = row.get(email_col)
            recipient_name = row.get('name', '') or row.get('Name', '')
            
            if not to_email:
                failed.append({'row': row, 'error': 'Missing email'})
                continue

            personalized_content = transform(email_data)
            personalized_subject = subject
            
            # Personalize content and subject
            for key, value in row.items():
                if key and value:
                    placeholder = f'[{key}]'
                    personalized_content = personalized_content.replace(placeholder, value)
                    personalized_subject = personalized_subject.replace(placeholder, value)

            try:
                msg = MIMEMultipart()
                msg['From'] = SMTP_USERNAME
                msg['To'] = to_email
                msg['Subject'] = personalized_subject  # Use dynamic subject
                msg.attach(MIMEText(personalized_content, 'html'))

                for file_path in attachments:
                    with open(file_path, 'rb') as f:
                        part = MIMEBase('application', 'octet-stream')
                        part.set_payload(f.read())
                        encoders.encode_base64(part)
                        part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(file_path)}"')
                        msg.attach(part)

                with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                    server.starttls()
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                    server.send_message(msg, from_addr=SMTP_USERNAME, to_addrs=[to_email])
                
                log_email_attempt(to_email, recipient_name, personalized_subject, 'success')
                sent += 1

            except Exception as smtp_e:
                error_msg = f'SMTP error: {str(smtp_e)}'
                log_email_attempt(to_email, recipient_name, personalized_subject, 'failure', error_msg)
                failed.append({'row': row, 'error': error_msg})

        for file_path in attachments:
            if os.path.exists(file_path):
                os.remove(file_path)

        return jsonify({'sent': sent, 'failed': failed, 'total': len(recipients)})

    except Exception as e:
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500

@app.route('/download-csv')
def download_csv():
    """Download CSV file with email logs"""
    date = request.args.get('date', datetime.datetime.now().strftime('%Y%m%d'))
    filename = f"email_logs_{date}.csv"
    filepath = os.path.join('csv_files', filename)
    
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True, download_name=filename)
    else:
        return jsonify({'error': 'CSV file not found'}), 404

@app.route('/download-logs')
def download_logs():
    """Download log file"""
    status = request.args.get('status', 'success')  # success or failure
    date = request.args.get('date', datetime.datetime.now().strftime('%Y%m%d'))
    filename = f"{status}_emails_{date}.txt"
    filepath = os.path.join('logs', filename)
    
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True, download_name=filename)
    else:
        return jsonify({'error': 'Log file not found'}), 404

@app.route('/get-mail-counts')
def get_mail_counts():
    """Get total, sent, and failed mail counts from CSV files"""
    total_count = 0
    sent_count = 0
    failed_count = 0
    
    if os.path.exists('csv_files'):
        for file in os.listdir('csv_files'):
            if file.endswith('.csv'):
                filepath = os.path.join('csv_files', file)
                try:
                    with open(filepath, 'r', encoding='utf-8') as csvfile:
                        reader = csv.DictReader(csvfile)
                        for row in reader:
                            total_count += 1
                            if row.get('status') == 'success':
                                sent_count += 1
                            elif row.get('status') == 'failure':
                                failed_count += 1
                except Exception as e:
                    app.logger.error(f"Error reading CSV file {file}: {e}")
    
    return jsonify({
        'total_count': total_count,
        'sent_count': sent_count,
        'failed_count': failed_count
    })

@app.route('/clear-data', methods=['POST'])
def clear_data():
    """Clear all logs and CSV files"""
    try:
        cleared_files = []
        
        # Clear CSV files
        if os.path.exists('csv_files'):
            for file in os.listdir('csv_files'):
                if file.endswith('.csv'):
                    filepath = os.path.join('csv_files', file)
                    os.remove(filepath)
                    cleared_files.append(f'csv_files/{file}')
        
        # Clear log files
        if os.path.exists('logs'):
            for file in os.listdir('logs'):
                if file.endswith('.txt'):
                    filepath = os.path.join('logs', file)
                    os.remove(filepath)
                    cleared_files.append(f'logs/{file}')
        
        return jsonify({
            'message': f'Cleared {len(cleared_files)} files',
            'cleared_files': cleared_files
        })
    
    except Exception as e:
        app.logger.error(f"Error clearing data: {e}")
        return jsonify({'error': f'Failed to clear data: {str(e)}'}), 500

@app.route('/list-files')
def list_files():
    """List available CSV and log files"""
    csv_files = []
    log_files = []
    
    # List CSV files
    if os.path.exists('csv_files'):
        for file in os.listdir('csv_files'):
            if file.endswith('.csv'):
                filepath = os.path.join('csv_files', file)
                size = os.path.getsize(filepath)
                modified = datetime.datetime.fromtimestamp(os.path.getmtime(filepath)).strftime('%Y-%m-%d %H:%M:%S')
                csv_files.append({
                    'name': file,
                    'size': size,
                    'modified': modified,
                    'download_url': f'/download-csv?date={file.replace("email_logs_", "").replace(".csv", "")}'
                })
    
    # List log files
    if os.path.exists('logs'):
        for file in os.listdir('logs'):
            if file.endswith('.txt'):
                filepath = os.path.join('logs', file)
                size = os.path.getsize(filepath)
                modified = datetime.datetime.fromtimestamp(os.path.getmtime(filepath)).strftime('%Y-%m-%d %H:%M:%S')
                status = 'success' if 'success' in file else 'failure'
                date = file.replace(f'{status}_emails_', '').replace('.txt', '')
                log_files.append({
                    'name': file,
                    'size': size,
                    'modified': modified,
                    'status': status,
                    'download_url': f'/download-logs?status={status}&date={date}'
                })
    
    return jsonify({
        'csv_files': csv_files,
        'log_files': log_files
    })

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy', 'service': 'EduTech AI Email Generator'}), 200

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
