from flask import Flask, request, jsonify, send_file, render_template, session, redirect, url_for, flash, make_response
from flask_cors import CORS
from flask_babel import Babel, gettext, ngettext, get_locale
try:
    from flask_socketio import SocketIO, emit, join_room, leave_room, rooms
except ImportError:
    print("Flask-SocketIO not installed. Install with: pip install flask-socketio")
    SocketIO = None
    emit = join_room = leave_room = rooms = None
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import json
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from config import Config
from utils.database import DatabaseManager
from utils.compression import compress_audio, compress_image, compress_pdf, get_file_type

# Initialize the database
db = DatabaseManager()

app = Flask(__name__)
app.config.from_object(Config)

# Initialize Babel
babel = Babel(app)

def get_locale():
    # Check if language is set in session
    if 'language' in session:
        return session['language']
    # Check if language is set in request args
    if request.args.get('lang'):
        return request.args.get('lang')
    # Default to English
    return request.accept_languages.best_match(app.config['LANGUAGES'].keys()) or app.config['BABEL_DEFAULT_LOCALE']

# Set the locale selector function
babel.locale_selector_func = get_locale

# Add template context processor for translations
@app.context_processor
def inject_translations():
    """Inject translation functions into templates"""
    from flask_babel import force_locale, gettext as _gettext
    
    def get_translation(text, locale=None):
        """Get translation for text in specified locale"""
        if locale and locale in app.config['LANGUAGES']:
            with force_locale(locale):
                return _gettext(text)
        return _gettext(text)
    
    return dict(get_translation=get_translation)

# Enhanced session security configuration
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this')
app.permanent_session_lifetime = timedelta(hours=8)  # Reduced from 7 days to 8 hours
app.config['SESSION_COOKIE_SECURE'] = True  # Only send over HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Prevent XSS attacks
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # CSRF protection
app.config['SESSION_COOKIE_NAME'] = 'digi_kul_session'  # Custom session cookie name
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

# Additional security headers
@app.after_request
def set_security_headers(response):
    """Set security headers for all responses"""
    # Prevent caching of sensitive pages
    if request.endpoint in ['teacher_dashboard', 'student_dashboard', 'admin_dashboard', 'teacher', 'student']:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    
    # Security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    return response

CORS(app, origins="*", supports_credentials=True)

if SocketIO:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
else:
    socketio = None

# Global session storage for active sessions
active_sessions = {}
session_participants = {}
online_users = {}

def cleanup_session(session_id):
    """Clean up session data when session ends"""
    if session_id in active_sessions:
        del active_sessions[session_id]
    if session_id in session_participants:
        del session_participants[session_id]

# Ensure upload directories exist
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'audio'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'images'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'documents'), exist_ok=True)
os.makedirs(os.path.join(app.config['COMPRESSED_FOLDER'], 'audio'), exist_ok=True)
os.makedirs(os.path.join(app.config['COMPRESSED_FOLDER'], 'images'), exist_ok=True)
os.makedirs(os.path.join(app.config['COMPRESSED_FOLDER'], 'documents'), exist_ok=True)

# Authentication Decorators
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if session exists and is valid
        if 'user_id' not in session or not session.get('user_id'):
            # Clear any invalid session data
            session.clear()
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login_page'))
        
        # Additional security: Check if user is still in online_users (session validation)
        user_id = session.get('user_id')
        if user_id not in online_users:
            session.clear()
            flash('Session expired. Please log in again.', 'error')
            return redirect(url_for('login_page'))
        
        return f(*args, **kwargs)
    return decorated_function

def teacher_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if session exists and is valid
        if 'user_id' not in session or not session.get('user_id'):
            session.clear()
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login_page'))
        
        # Check user type
        if session.get('user_type') != 'teacher':
            session.clear()
            flash('Access denied. Teachers only.', 'error')
            return redirect(url_for('login_page'))
        
        # Additional security: Check if user is still in online_users
        user_id = session.get('user_id')
        if user_id not in online_users:
            session.clear()
            flash('Session expired. Please log in again.', 'error')
            return redirect(url_for('login_page'))
        
        return f(*args, **kwargs)
    return decorated_function

def api_teacher_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if session exists and is valid
        if 'user_id' not in session or not session.get('user_id'):
            return jsonify({'error': 'Please log in to access this API'}), 401
        
        # Check user type
        if session.get('user_type') != 'teacher':
            return jsonify({'error': 'Access denied. Teachers only.'}), 403
        
        # Additional security: Check if user is still in online_users
        user_id = session.get('user_id')
        if user_id not in online_users:
            return jsonify({'error': 'Session expired. Please log in again.'}), 401
        
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if session exists and is valid
        if 'user_id' not in session or not session.get('user_id'):
            session.clear()
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login_page'))
        
        # Check user type
        if session.get('user_type') != 'admin':
            session.clear()
            flash('Access denied. Admin only.', 'error')
            return redirect(url_for('login_page'))
        
        return f(*args, **kwargs)
    return decorated_function

def api_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if session exists and is valid
        if 'user_id' not in session or not session.get('user_id'):
            return jsonify({'error': 'Please log in to access this API'}), 401
        
        # Check user type
        if session.get('user_type') != 'admin':
            return jsonify({'error': 'Access denied. Admin only.'}), 403
        
        return f(*args, **kwargs)
    return decorated_function

def api_student_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if session exists and is valid
        if 'user_id' not in session or not session.get('user_id'):
            return jsonify({'error': 'Please log in to access this API'}), 401
        
        # Check user type
        if session.get('user_type') != 'student':
            return jsonify({'error': 'Access denied. Students only.'}), 403
        
        return f(*args, **kwargs)
    return decorated_function

def student_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if session exists and is valid
        if 'user_id' not in session or not session.get('user_id'):
            session.clear()
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login_page'))
        
        # Check user type
        if session.get('user_type') != 'student':
            session.clear()
            flash('Access denied. Students only.', 'error')
            return redirect(url_for('login_page'))
        
        # Additional security: Check if user is still in online_users
        user_id = session.get('user_id')
        if user_id not in online_users:
            session.clear()
            flash('Session expired. Please log in again.', 'error')
            return redirect(url_for('login_page'))
        
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if session exists and is valid
        if 'user_id' not in session or not session.get('user_id'):
            session.clear()
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login_page'))
        
        # Check user type
        if session.get('user_type') != 'admin':
            session.clear()
            flash('Access denied. Admin only.', 'error')
            return redirect(url_for('login_page'))
        
        # Additional security: Check if user is still in online_users
        user_id = session.get('user_id')
        if user_id not in online_users:
            session.clear()
            flash('Session expired. Please log in again.', 'error')
            return redirect(url_for('login_page'))
        
        return f(*args, **kwargs)
    return decorated_function

# API Authentication for mobile/AJAX requests
def api_auth_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check session first with enhanced validation
        if 'user_id' in session and session.get('user_id'):
            user_id = session.get('user_id')
            # Additional security: Check if user is still in online_users
            if user_id not in online_users:
                session.clear()
                return jsonify({'error': 'Session expired. Please log in again.'}), 401
            return f(*args, **kwargs)
        
        # Check Authorization header for API requests
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            # Validate token (implement your token validation logic)
            user_id = validate_token(token)
            if user_id:
                return f(*args, **kwargs)
        
        return jsonify({'error': 'Authentication required'}), 401
    return decorated_function

def validate_token(token):
    """Validate JWT token and return user_id if valid"""
    try:
        # Implement JWT validation here
        # For now, return None (tokens not implemented yet)
        return None
    except:
        return None

# Routes - Authentication
@app.route('/')
def index():
    """Landing page"""
    return render_template('index.html')

@app.route('/set_language/<language>')
def set_language(language=None):
    """Set the language for the session"""
    if language and language in app.config['LANGUAGES']:
        session['language'] = language
    return redirect(request.referrer or url_for('index'))

@app.route('/login')
def login_page():
    """Login page"""
    return render_template('login.html')

@app.route('/admin_login')
def admin_login_page():
    """Admin-only login page"""
    return render_template('login.html', admin_mode=True)

@app.route('/register')
def register_page():
    """Registration page"""
    return render_template('register.html')

@app.route('/api/register/teacher', methods=['POST'])
@api_admin_required
def register_teacher():
    """Register a new teacher"""
    try:
        data = request.get_json()
        
        if not all(key in data for key in ['name', 'email', 'institution', 'subject', 'password']):
            return jsonify({'error': 'Missing required fields'}), 400
        
        # Hash password
        password_hash = generate_password_hash(data['password'])
        
        teacher_id, response = db.create_teacher(
            data['name'], data['email'], data['institution'], 
            data['subject'], password_hash
        )
        
        if teacher_id:
            return jsonify({
                'success': True,
                'message': 'Teacher registered successfully',
                'teacher_id': teacher_id
            }), 201
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/register/admin', methods=['POST'])
def register_admin():
    """Admin registration disabled: only one hardcoded admin is allowed"""
    return jsonify({'error': 'Admin registration is disabled'}), 403

@app.route('/api/register/student', methods=['POST'])
@api_admin_required
def register_student():
    """Register a new student"""
    try:
        data = request.get_json()
        
        if not all(key in data for key in ['name', 'email', 'institution', 'password']):
            return jsonify({'error': 'Missing required fields'}), 400
        
        # Hash password
        password_hash = generate_password_hash(data['password'])
        
        student_id, response = db.create_student(
            data['name'], data['email'], data['institution'], password_hash
        )
        
        if student_id:
            return jsonify({
                'success': True,
                'message': 'Student registered successfully',
                'student_id': student_id
            }), 201
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/login', methods=['POST'])
def login():
    """Login for teachers and students"""
    try:
        data = request.get_json()
        
        if not all(key in data for key in ['email', 'password', 'user_type']):
            return jsonify({'error': 'Missing required fields'}), 400
        
        email = data['email']
        password = data['password']
        user_type = data['user_type']  # 'teacher' or 'student'
        
        if user_type == 'teacher':
            user = db.get_teacher_by_email(email)
        elif user_type == 'student':
            user = db.get_student_by_email(email)
        elif user_type == 'admin':
            # Enforce single hardcoded admin
            if email != 'Admin@gmail.com' or password != 'Admin@#1234':
                return jsonify({'error': 'Invalid admin credentials'}), 401
            # Success: set session without DB
            session.permanent = True
            session['user_id'] = 'admin'
            session['user_type'] = 'admin'
            session['user_name'] = 'Admin'
            session['user_email'] = 'admin@local'
            online_users['admin'] = {
                'name': 'Admin',
                'email': 'admin@local',
                'type': 'admin',
                'login_time': datetime.now().isoformat()
            }
            return jsonify({
                'success': True,
                'message': 'Login successful',
                'user_type': 'admin',
                'redirect_url': '/admin_dashboard'
            }), 200
        else:
            return jsonify({'error': 'Invalid user type'}), 400
        
        if user_type != 'admin' and not user:
            return jsonify({'error': 'User not found'}), 404
        
        if user_type != 'admin' and not check_password_hash(user['password_hash'], password):
            return jsonify({'error': 'Invalid password'}), 401
        
        if user_type != 'admin':
            # Create session for teacher/student
            session.permanent = True
            session['user_id'] = user['id']
            session['user_type'] = user_type
            session['user_name'] = user['name']
            session['user_email'] = user['email']
            
            # Track online users
            online_users[user['id']] = {
                'name': user['name'],
                'email': user['email'],
                'type': user_type,
                'login_time': datetime.now().isoformat()
            }
            
            return jsonify({
                'success': True,
                'message': 'Login successful',
                'user_type': user_type,
                'redirect_url': f'/{user_type}_dashboard'
            }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/logout', methods=['POST'])
def logout():
    """Secure logout with complete session destruction"""
    try:
        user_id = session.get('user_id')
        user_type = session.get('user_type')
        
        # Remove from online users tracking
        if user_id and user_id in online_users:
            del online_users[user_id]
        
        # Clear all session data
        session.clear()
        
        # Force session expiration by setting a past date
        session.permanent = False
        
        # Create response with security headers
        response = jsonify({
            'success': True,
            'message': 'Logged out successfully',
            'redirect_url': '/',
            'logout_timestamp': datetime.now().isoformat()
        })
        
        # Set session cookie to expire immediately
        response.set_cookie(
            app.config['SESSION_COOKIE_NAME'], 
            '', 
            expires=0,
            secure=app.config['SESSION_COOKIE_SECURE'],
            httponly=app.config['SESSION_COOKIE_HTTPONLY'],
            samesite=app.config['SESSION_COOKIE_SAMESITE']
        )
        
        # Additional security headers for logout
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        
        return response, 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/logout', methods=['GET', 'POST'])
def logout_page():
    """Logout page with additional security measures"""
    try:
        user_id = session.get('user_id')
        user_type = session.get('user_type')
        
        # Remove from online users tracking
        if user_id and user_id in online_users:
            del online_users[user_id]
        
        # Clear all session data
        session.clear()
        session.permanent = False
        
        # Create response
        response = make_response(render_template('logout.html'))
        
        # Set session cookie to expire immediately
        response.set_cookie(
            app.config['SESSION_COOKIE_NAME'], 
            '', 
            expires=0,
            secure=app.config['SESSION_COOKIE_SECURE'],
            httponly=app.config['SESSION_COOKIE_HTTPONLY'],
            samesite=app.config['SESSION_COOKIE_SAMESITE']
        )
        
        # Security headers
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        
        return response
        
    except Exception as e:
        return redirect(url_for('login_page'))

@app.route('/api/validate-session', methods=['GET'])
def validate_session():
    """Validate current session and return user info"""
    try:
        if 'user_id' not in session or not session.get('user_id'):
            return jsonify({'valid': False, 'error': 'No active session'}), 401
        
        user_id = session.get('user_id')
        
        # Check if user is still in online_users
        if user_id not in online_users:
            session.clear()
            return jsonify({'valid': False, 'error': 'Session expired'}), 401
        
        return jsonify({
            'valid': True,
            'user_id': user_id,
            'user_type': session.get('user_type'),
            'user_name': session.get('user_name'),
            'user_email': session.get('user_email')
        }), 200
        
    except Exception as e:
        return jsonify({'valid': False, 'error': str(e)}), 500

@app.route('/api/force-logout', methods=['POST'])
def force_logout():
    """Force logout all sessions for a user (admin function)"""
    try:
        if session.get('user_type') != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        
        data = request.get_json()
        target_user_id = data.get('user_id')
        
        if not target_user_id:
            return jsonify({'error': 'User ID required'}), 400
        
        # Remove from online users
        if target_user_id in online_users:
            del online_users[target_user_id]
        
        return jsonify({
            'success': True,
            'message': f'User {target_user_id} logged out successfully'
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Dashboard Routes
@app.route('/teacher_dashboard')
@teacher_required
def teacher_dashboard():
    """Teacher dashboard"""
    return render_template('teacher_dashboard.html', 
                         user_name=session.get('user_name'),
                         user_email=session.get('user_email'))

@app.route('/student_dashboard')
@student_required
def student_dashboard():
    """Student dashboard"""
    return render_template('student_dashboard.html',
                         user_name=session.get('user_name'),
                         user_email=session.get('user_email'))

@app.route('/admin_dashboard')
@admin_required
def admin_dashboard():
    """Admin dashboard"""
    return render_template('admin_dashboard.html',
                         user_name=session.get('user_name'),
                         user_email=session.get('user_email'))

@app.route('/student/<student_id>')
@student_required
def student_profile(student_id):
    """Individual student profile page - redirects to dashboard"""
    if session.get('user_id') != student_id:
        flash('Access denied.', 'error')
        return redirect(url_for('student_dashboard'))
    
    # Redirect to student dashboard since individual profile page is not implemented
    flash('Profile page not available. Redirecting to dashboard.', 'info')
    return redirect(url_for('student_dashboard'))

# Teacher APIs
@app.route('/api/teacher/lectures', methods=['POST'])
@api_teacher_required
def create_lecture():
    """Create a new lecture schedule"""
    try:
        data = request.get_json()
        
        if not all(key in data for key in ['title', 'description', 'scheduled_time', 'duration']):
            return jsonify({'error': 'Missing required fields'}), 400
        
        lecture_id, response = db.create_lecture(
            session['user_id'], data['title'], data['description'], 
            data['scheduled_time'], data['duration']
        )
        
        if lecture_id:
            # Notify all students about new lecture
            if socketio:
                socketio.emit('new_lecture', {
                    'lecture_id': lecture_id,
                    'title': data['title'],
                    'teacher_name': session['user_name'],
                    'scheduled_time': data['scheduled_time']
                }, namespace='/', room='students')
            
            return jsonify({
                'success': True,
                'message': 'Lecture created successfully',
                'lecture_id': lecture_id
            }), 201
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/teacher/lectures', methods=['GET'])
@api_teacher_required
def get_teacher_lectures():
    """Get all lectures for current teacher"""
    try:
        lectures = db.get_teacher_lectures(session['user_id'])
        return jsonify({
            'success': True,
            'lectures': lectures
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/teacher/upload_material', methods=['POST'])
@api_teacher_required
def upload_material():
    """Upload teaching material with automatic compression"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        lecture_id = request.form.get('lecture_id')
        title = request.form.get('title', 'Untitled')
        description = request.form.get('description', '')
        
        if not lecture_id:
            return jsonify({'error': 'Lecture ID is required'}), 400
        
        # Verify teacher owns this lecture
        lecture = db.get_lecture_by_id(lecture_id)
        if not lecture or lecture['teacher_id'] != session['user_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        # Process file upload (same as before)
        filename = secure_filename(file.filename)
        file_type = get_file_type(filename)
        
        upload_subdir = os.path.join(app.config['UPLOAD_FOLDER'], file_type + 's')
        compressed_subdir = os.path.join(app.config['COMPRESSED_FOLDER'], file_type + 's')
        os.makedirs(upload_subdir, exist_ok=True)
        os.makedirs(compressed_subdir, exist_ok=True)
        
        original_filename = f"{uuid.uuid4()}_{filename}"
        original_path = os.path.join(upload_subdir, original_filename)
        file.save(original_path)
        
        compressed_filename = f"compressed_{original_filename}"
        compressed_path = os.path.join(compressed_subdir, compressed_filename)
        
        original_size = os.path.getsize(original_path)
        
        if file_type == 'audio':
            compressed_size = compress_audio(original_path, compressed_path)
        elif file_type == 'image':
            compressed_size = compress_image(original_path, compressed_path)
        elif file_type == 'document':
            if filename.lower().endswith('.pdf'):
                compressed_size = compress_pdf(original_path, compressed_path)
            else:
                with open(original_path, 'rb') as f_in:
                    with open(compressed_path, 'wb') as f_out:
                        f_out.write(f_in.read())
                compressed_size = original_size
        else:
            with open(original_path, 'rb') as f_in:
                with open(compressed_path, 'wb') as f_out:
                    f_out.write(f_in.read())
            compressed_size = original_size
        
        material_id, response = db.add_material(
            lecture_id, title, description, original_path, 
            compressed_path, compressed_size, file_type
        )
        
        if material_id:
            # Notify students about new material
            if socketio:
                socketio.emit('new_material', {
                    'lecture_id': lecture_id,
                    'material_id': material_id,
                    'title': title,
                    'file_type': file_type,
                    'teacher_name': session['user_name']
                }, namespace='/', room='students')
            
            return jsonify({
                'success': True,
                'message': 'File uploaded and compressed successfully',
                'material_id': material_id,
                'original_size': original_size,
                'compressed_size': compressed_size,
                'compression_ratio': f"{((original_size - compressed_size) / original_size * 100):.2f}%"
            }), 201
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/teacher/live_session/start', methods=['POST'])
@api_teacher_required
def start_live_session():
    """Start a live session for a lecture"""
    try:
        data = request.get_json()
        lecture_id = data.get('lecture_id')
        
        if not lecture_id:
            return jsonify({'error': 'Lecture ID is required'}), 400
        
        # Verify teacher owns this lecture
        lecture = db.get_lecture_by_id(lecture_id)
        if not lecture or lecture['teacher_id'] != session['user_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        session_id = f"session_{lecture_id}_{uuid.uuid4().hex[:8]}"
        
        active_sessions[session_id] = {
            'lecture_id': lecture_id,
            'teacher_id': session['user_id'],
            'teacher_name': session['user_name'],
            'lecture_title': lecture['title'],
            'started_at': datetime.now().isoformat(),
            'status': 'active',
            'participants': [],
            'recordings': []
        }
        
        # Notify all students about live session
        if socketio:
            socketio.emit('live_session_started', {
                'session_id': session_id,
                'lecture_id': lecture_id,
                'lecture_title': lecture['title'],
                'teacher_name': session['user_name'],
                'join_url': f"/student/join_session/{session_id}"
            }, namespace='/', room='students')
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'message': 'Live session started successfully'
        }), 201
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Student APIs
@app.route('/api/student/lectures/available', methods=['GET'])
@api_student_required
def get_available_lectures():
    """Get all available lectures for students"""
    try:
        lectures = db.get_all_lectures()
        current_time = datetime.now().isoformat()
        
        available_lectures = []
        for lecture in lectures:
            # Add session status
            session_active = any(
                session['lecture_id'] == lecture['id'] and session['status'] == 'active'
                for session in active_sessions.values()
            )
            lecture['session_active'] = session_active
            lecture['can_join'] = True
            
            # Get teacher info
            teacher = db.get_teacher_by_id(lecture['teacher_id'])
            if teacher:
                lecture['teacher_name'] = teacher['name']
                lecture['teacher_institution'] = teacher['institution']
            
            available_lectures.append(lecture)
        
        return jsonify({
            'success': True,
            'lectures': available_lectures
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/by_lecture/<lecture_id>', methods=['GET'])
@api_auth_required
def get_session_by_lecture(lecture_id):
    """Return active session id for a given lecture if exists"""
    try:
        for sid, info in active_sessions.items():
            if info.get('lecture_id') == lecture_id and info.get('status') == 'active':
                return jsonify({'success': True, 'session_id': sid}), 200
        return jsonify({'error': 'No active session for this lecture'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500
@app.route('/api/student/lecture/<lecture_id>/materials', methods=['GET'])
@api_student_required
def get_lecture_materials(lecture_id):
    """Get materials for a lecture"""
    try:
        materials = db.get_lecture_materials(lecture_id)
        
        # Add download URLs and file info
        for material in materials:
            material['download_url'] = f"/api/download/{material['id']}"
            material['file_size_mb'] = round(material['file_size'] / (1024 * 1024), 2)
        
        return jsonify({
            'success': True,
            'materials': materials
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/teacher/lecture/<lecture_id>/materials', methods=['GET'])
@api_teacher_required
def get_teacher_lecture_materials(lecture_id):
    """Get materials for a lecture (teacher view)"""
    try:
        # Verify teacher owns this lecture
        lecture = db.get_lecture_by_id(lecture_id)
        if not lecture or lecture['teacher_id'] != session['user_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        materials = db.get_lecture_materials(lecture_id)
        
        # Add download URLs and file info
        for material in materials:
            material['download_url'] = f"/api/download/{material['id']}"
            material['file_size_mb'] = round(material['file_size'] / (1024 * 1024), 2)
        
        return jsonify({
            'success': True,
            'materials': materials
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/teacher/materials/<material_id>', methods=['DELETE'])
@api_teacher_required
def delete_teacher_material(material_id):
    """Delete a material (teacher only)"""
    try:
        # Get material details to verify ownership
        material = db.get_material_by_id(material_id)
        if not material:
            return jsonify({'error': 'Material not found'}), 404
        
        # Verify teacher owns the lecture this material belongs to
        lecture = db.get_lecture_by_id(material['lecture_id'])
        if not lecture or lecture['teacher_id'] != session['user_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        success = db.delete_material(material_id)
        if success:
            return jsonify({'success': True, 'message': 'Material deleted successfully'}), 200
        else:
            return jsonify({'error': 'Failed to delete material'}), 500
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/enroll', methods=['POST'])
@api_student_required
def enroll_in_lecture():
    """Enroll student in a lecture"""
    try:
        data = request.get_json()
        lecture_id = data.get('lecture_id')
        
        if not lecture_id:
            return jsonify({'error': 'Lecture ID is required'}), 400
        
        # Check if lecture exists
        lecture = db.get_lecture_by_id(lecture_id)
        if not lecture:
            return jsonify({'error': 'Lecture not found'}), 404
        
        # Check if already enrolled
        already_enrolled = db.is_student_enrolled(session['user_id'], lecture_id)
        if already_enrolled:
            return jsonify({
                'success': True,
                'message': 'Already enrolled in this lecture',
                'already_enrolled': True
            }), 200
        
        # Enroll student
        enrollment_id, response = db.enroll_student(
            session['user_id'], lecture_id
        )
        
        if enrollment_id:
            return jsonify({
                'success': True,
                'message': 'Enrolled successfully',
                'enrollment_id': enrollment_id
            }), 201
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/enrolled_lectures', methods=['GET'])
@api_student_required
def get_enrolled_lectures():
    """Get lectures student is enrolled in"""
    try:
        lectures = db.get_student_enrolled_lectures(session['user_id'])
        
        for lecture in lectures:
            # Add session status
            session_active = any(
                session['lecture_id'] == lecture['id'] and session['status'] == 'active'
                for session in active_sessions.values()
            )
            lecture['session_active'] = session_active
            
            # Get teacher info
            teacher = db.get_teacher_by_id(lecture['teacher_id'])
            if teacher:
                lecture['teacher_name'] = teacher['name']
        
        return jsonify({
            'success': True,
            'lectures': lectures
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/student/join_session/<session_id>')
@student_required
def join_session_page(session_id):
    """Join live session page for students"""
    if session_id not in active_sessions:
        flash('Session not found or has ended.', 'error')
        return redirect(url_for('student_dashboard'))
    
    session_info = active_sessions[session_id]
    return render_template('student_live_session.html', lecture_id=session_info['lecture_id'])

@app.route('/teacher/manage_session/<session_id>')
@teacher_required
def manage_session_page(session_id):
    """Manage live session page for teachers"""
    if session_id not in active_sessions:
        flash('Session not found.', 'error')
        return redirect(url_for('teacher_dashboard'))
    
    session_info = active_sessions[session_id]
    if session_info['teacher_id'] != session['user_id']:
        flash('Access denied.', 'error')
        return redirect(url_for('teacher_dashboard'))
    
    return render_template('teacher_live_session.html', lecture_id=session_info['lecture_id'])

# File Download
@app.route('/api/download/<material_id>')
@login_required
def download_material(material_id):
    """Download teaching material"""
    try:
        material = db.get_material_details(material_id)
        
        if not material:
            return jsonify({'error': 'Material not found'}), 404
        
        # For students, check if they're enrolled in the lecture
        if session.get('user_type') == 'student':
            enrolled = db.is_student_enrolled(
                session['user_id'], material['lecture_id']
            )
            if not enrolled:
                return jsonify({'error': 'Not enrolled in this lecture'}), 403
        
        # For teachers, check if they own the lecture
        elif session.get('user_type') == 'teacher':
            lecture = db.get_lecture_by_id(material['lecture_id'])
            if not lecture or lecture['teacher_id'] != session['user_id']:
                return jsonify({'error': 'Unauthorized'}), 403
        
        if not os.path.exists(material['compressed_path']):
            return jsonify({'error': 'File not found'}), 404
        
        return send_file(material['compressed_path'], as_attachment=True)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# WebSocket Events for Real-time Updates
if socketio:
    @socketio.on('connect')
    def handle_connect():
        if 'user_id' in session:
            user_type = session.get('user_type')
            join_room(user_type + 's')  # Join 'teachers' or 'students' room
            
            emit('connected', {
                'message': f'Connected as {user_type}',
                'user_id': session['user_id']
            })
    
    @socketio.on('disconnect')
    def handle_disconnect():
        if 'user_id' in session:
            user_type = session.get('user_type')
            leave_room(user_type + 's')
    
    # Live session socket events (keep existing ones)
    @socketio.on('join_session')
    def handle_join_session(data):
        session_id = data.get('session_id')
        
        if 'user_id' not in session:
            emit('error', {'message': 'Authentication required'})
            return
        
        user_id = session['user_id']
        user_type = session['user_type']
        user_name = session['user_name']
        
        if session_id not in active_sessions:
            emit('error', {'message': 'Session not found'})
            return
        
        join_room(session_id)
        
        if session_id not in session_participants:
            session_participants[session_id] = {}
        
        session_participants[session_id][user_id] = {
            'user_type': user_type,
            'user_name': user_name,
            'socket_id': request.sid,
            'joined_at': datetime.now().isoformat()
        }
        
        emit('user_joined', {
            'user_id': user_id,
            'user_name': user_name,
            'user_type': user_type,
            'participants_count': len(session_participants[session_id])
        }, room=session_id, include_self=False)
        
        emit('session_info', {
            'session_id': session_id,
            'participants': list(session_participants[session_id].values()),
            'participants_count': len(session_participants[session_id])
        })

    @socketio.on('leave_session')
    def handle_leave_session(data):
        session_id = data.get('session_id')
        user_id = data.get('user_id') or (session.get('user_id') if 'user_id' in session else None)
        if not session_id or not user_id:
            return
        leave_room(session_id)
        if session_id in session_participants and user_id in session_participants[session_id]:
            del session_participants[session_id][user_id]
            participants_count = len(session_participants[session_id])
            emit('user_left', {
                'user_id': user_id,
                'participants_count': participants_count
            }, room=session_id)
            
            # If no participants left, end the session
            if participants_count == 0:
                if session_id in active_sessions:
                    active_sessions[session_id]['status'] = 'ended'
                    emit('session_ended', {}, room=session_id)
                    # Clean up after a delay
                    threading.Timer(5.0, lambda: cleanup_session(session_id)).start()

    @socketio.on('webrtc_offer')
    def handle_webrtc_offer(data):
        session_id = data.get('session_id')
        target_user_id = data.get('target_user_id')
        offer = data.get('offer')
        from_user_id = data.get('from_user_id')
        if not session_id or not target_user_id or session_id not in session_participants:
            emit('error', {'message': 'Invalid signaling data'})
            return
        target = session_participants[session_id].get(target_user_id)
        if not target:
            emit('error', {'message': 'Target not found'})
            return
        emit('webrtc_offer', {
            'from_user_id': from_user_id,
            'offer': offer
        }, room=target['socket_id'])

    @socketio.on('webrtc_answer')
    def handle_webrtc_answer(data):
        session_id = data.get('session_id')
        target_user_id = data.get('target_user_id')
        answer = data.get('answer')
        from_user_id = data.get('from_user_id')
        if not session_id or not target_user_id or session_id not in session_participants:
            emit('error', {'message': 'Invalid signaling data'})
            return
        target = session_participants[session_id].get(target_user_id)
        if not target:
            emit('error', {'message': 'Target not found'})
            return
        emit('webrtc_answer', {
            'from_user_id': from_user_id,
            'answer': answer
        }, room=target['socket_id'])

    @socketio.on('ice_candidate')
    def handle_ice_candidate(data):
        session_id = data.get('session_id')
        target_user_id = data.get('target_user_id')
        candidate = data.get('candidate')
        from_user_id = data.get('from_user_id')
        if not session_id or not target_user_id or session_id not in session_participants:
            emit('error', {'message': 'Invalid signaling data'})
            return
        target = session_participants[session_id].get(target_user_id)
        if not target:
            emit('error', {'message': 'Target not found'})
            return
        emit('ice_candidate', {
            'from_user_id': from_user_id,
            'candidate': candidate
        }, room=target['socket_id'])

    @socketio.on('chat_message')
    def handle_chat_message(data):
        session_id = data.get('session_id')
        if not session_id:
            return
        emit('chat_message', data, room=session_id)

    @socketio.on('quality_report')
    def handle_quality_report(data):
        # In the future, persist or analyze quality reports
        pass

# Admin API Routes
@app.route('/api/admin/cohorts', methods=['GET'])
@api_admin_required
def get_all_cohorts():
    """Get all cohorts"""
    try:
        cohorts = db.get_all_cohorts()
        return jsonify({
            'success': True,
            'cohorts': cohorts
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/cohorts', methods=['POST'])
@api_admin_required
def create_cohort():
    """Create a new cohort"""
    try:
        data = request.get_json()
        
        if not all(key in data for key in ['name', 'subject', 'teacher_id']):
            return jsonify({'error': 'Missing required fields'}), 400
        
        description = data.get('description', '')
        cohort_id, response = db.create_cohort(
            data['name'], description, data['subject'], data['teacher_id']
        )
        
        if cohort_id:
            return jsonify({
                'success': True,
                'message': 'Cohort created successfully',
                'cohort_id': cohort_id
            }), 201
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/cohort/<cohort_id>/students', methods=['GET'])
@admin_required
def admin_get_cohort_students(cohort_id):
    try:
        students = db.get_cohort_students(cohort_id)
        return jsonify({'success': True, 'students': students}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/cohort/<cohort_id>/students', methods=['POST'])
@admin_required
def admin_add_student_to_cohort(cohort_id):
    try:
        data = request.get_json()
        student_id = data.get('student_id')
        student_email = data.get('student_email')
        if not student_id and not student_email:
            return jsonify({'error': 'student_id or student_email is required'}), 400
        if not student_id and student_email:
            student = db.get_student_by_email(student_email)
            if not student:
                return jsonify({'error': 'Student not found'}), 404
            student_id = student['id']
        success, msg = db.add_student_to_cohort(cohort_id, student_id)
        if success:
            return jsonify({'success': True, 'message': msg}), 200
        return jsonify({'error': msg}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/cohort/<cohort_id>/students/<student_id>', methods=['DELETE'])
@admin_required
def admin_remove_student_from_cohort(cohort_id, student_id):
    try:
        success, msg = db.remove_student_from_cohort(cohort_id, student_id)
        if success:
            return jsonify({'success': True, 'message': msg}), 200
        return jsonify({'error': msg}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/cohorts/<cohort_id>', methods=['DELETE'])
@api_admin_required
def delete_cohort(cohort_id):
    """Delete a cohort"""
    try:
        success, response = db.delete_cohort(cohort_id)
        
        if success:
            return jsonify({
                'success': True,
                'message': 'Cohort deleted successfully'
            }), 200
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/teachers', methods=['GET'])
@admin_required
def get_all_teachers():
    """Get all teachers"""
    try:
        teachers = db.get_all_teachers()
        return jsonify({
            'success': True,
            'teachers': teachers
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/students', methods=['GET'])
@admin_required
def get_all_students():
    """Get all students"""
    try:
        students = db.get_all_students()
        return jsonify({
            'success': True,
            'students': students
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/cohort/<cohort_id>/lectures', methods=['GET'])
@api_admin_required
def get_admin_cohort_lectures(cohort_id):
    """Get lectures for a cohort (admin access)"""
    try:
        lectures = db.get_cohort_lectures(cohort_id)
        
        for lecture in lectures:
            # Add session status
            session_active = any(
                session['lecture_id'] == lecture['id'] and session['status'] == 'active'
                for session in active_sessions.values()
            )
            lecture['session_active'] = session_active
        
        return jsonify({
            'success': True,
            'lectures': lectures
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Student Cohort Routes
@app.route('/api/student/cohorts', methods=['GET'])
@api_student_required
def get_student_cohorts():
    """Get cohorts for current student"""
    try:
        cohorts = db.get_student_cohorts(session['user_id'])
        return jsonify({
            'success': True,
            'cohorts': cohorts
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/cohorts/join', methods=['POST'])
@api_student_required
def join_cohort():
    """Student joins a cohort"""
    try:
        data = request.get_json()
        cohort_code = data.get('cohort_code')
        
        if not cohort_code:
            return jsonify({'error': 'Cohort code is required'}), 400
        
        success, response = db.join_cohort_by_code(session['user_id'], cohort_code)
        
        if success:
            return jsonify({
                'success': True,
                'message': 'Successfully joined cohort'
            }), 200
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/cohort/<cohort_id>/lectures', methods=['GET'])
@api_student_required
def get_cohort_lectures(cohort_id):
    """Get lectures for a specific cohort"""
    try:
        # Verify student is in this cohort
        in_cohort = db.is_student_in_cohort(session['user_id'], cohort_id)
        if not in_cohort:
            return jsonify({'error': 'Not enrolled in this cohort'}), 403
        
        lectures = db.get_cohort_lectures(cohort_id)
        
        for lecture in lectures:
            # Add session status
            session_active = any(
                session['lecture_id'] == lecture['id'] and session['status'] == 'active'
                for session in active_sessions.values()
            )
            lecture['session_active'] = session_active
            
            # Get teacher info
            teacher = db.get_teacher_by_id(lecture['teacher_id'])
            if teacher:
                lecture['teacher_name'] = teacher['name']
        
        return jsonify({
            'success': True,
            'lectures': lectures
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/student/polls', methods=['GET'])
@api_student_required
def get_student_polls():
    """Get all polls available to a student"""
    try:
        # Get student's cohorts
        cohorts = db.get_student_cohorts(session['user_id'])
        cohort_ids = [cohort['id'] for cohort in cohorts]
        
        # Get polls from all cohorts
        all_polls = []
        for cohort_id in cohort_ids:
            polls = db.get_cohort_polls(cohort_id)
            all_polls.extend(polls)
        
        # Remove duplicates based on poll ID
        unique_polls = []
        seen_ids = set()
        for poll in all_polls:
            if poll['id'] not in seen_ids:
                unique_polls.append(poll)
                seen_ids.add(poll['id'])
        
        return jsonify({
            'success': True,
            'polls': unique_polls
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/lecture_id', methods=['GET'])
@api_auth_required
def get_session_lecture_id():
    """Get lecture ID from current session"""
    try:
        # This is a placeholder - in a real implementation, you'd get this from the session
        # For now, we'll return an error as this should be handled by the template
        return jsonify({'error': 'Lecture ID should be provided by template'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Teacher Cohort Routes
@app.route('/api/teacher/cohorts', methods=['GET'])
@api_teacher_required
def get_teacher_cohorts():
    """Get cohorts for current teacher"""
    try:
        cohorts = db.get_teacher_cohorts(session['user_id'])
        return jsonify({
            'success': True,
            'cohorts': cohorts
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/teacher/cohort/<cohort_id>/students', methods=['GET'])
@api_teacher_required
def get_cohort_students(cohort_id):
    """Get students in a cohort for teachers"""
    try:
        # Verify teacher owns this cohort
        cohort = db.get_cohort_by_id(cohort_id)
        if not cohort or cohort['teacher_id'] != session['user_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        students = db.get_cohort_students(cohort_id)
        return jsonify({
            'success': True,
            'students': students
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/teacher/cohort/<cohort_id>/lectures', methods=['POST'])
@api_teacher_required
def create_cohort_lecture(cohort_id):
    """Create a lecture for a specific cohort"""
    try:
        data = request.get_json()
        
        if not all(key in data for key in ['title', 'description', 'scheduled_time', 'duration']):
            return jsonify({'error': 'Missing required fields'}), 400
        
        # Verify teacher owns this cohort
        cohort = db.get_cohort_by_id(cohort_id)
        if not cohort or cohort['teacher_id'] != session['user_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        lecture_id, response = db.create_lecture_for_cohort(
            cohort_id, session['user_id'], data['title'], data['description'], 
            data['scheduled_time'], data['duration']
        )
        
        if lecture_id:
            # Notify all students in the cohort about new lecture
            if socketio:
                socketio.emit('new_lecture', {
                    'lecture_id': lecture_id,
                    'title': data['title'],
                    'teacher_name': session['user_name'],
                    'scheduled_time': data['scheduled_time'],
                    'cohort_id': cohort_id
                }, namespace='/', room=f'cohort_{cohort_id}')
            
            return jsonify({
                'success': True,
                'message': 'Lecture created successfully',
                'lecture_id': lecture_id
            }), 201
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Health Check
# Poll System APIs
@app.route('/api/polls', methods=['POST'])
@api_auth_required
def create_poll():
    """Create a new poll"""
    try:
        data = request.get_json()
        
        if not all(key in data for key in ['question', 'options', 'lecture_id']):
            return jsonify({'error': 'Missing required fields'}), 400
        
        poll_id, response = db.create_poll(
            data['lecture_id'], 
            data['question'], 
            data['options'],
            session['user_id']
        )
        
        if poll_id:
            return jsonify({
                'success': True,
                'message': 'Poll created successfully',
                'poll_id': poll_id
            }), 201
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/polls/<poll_id>/vote', methods=['POST'])
@api_student_required
def vote_on_poll(poll_id):
    """Vote on a poll"""
    try:
        data = request.get_json()
        
        if 'response' not in data:
            return jsonify({'error': 'Response is required'}), 400
        
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'error': 'User not authenticated'}), 401
        
        response_id, message = db.submit_poll_response(user_id, poll_id, data['response'])
        
        if response_id:
            return jsonify({
                'success': True,
                'message': 'Vote submitted successfully'
            }), 200
        else:
            return jsonify({'error': message}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/polls/<poll_id>/results', methods=['GET'])
@api_auth_required
def get_poll_results(poll_id):
    """Get poll results"""
    try:
        results = db.get_poll_results(poll_id)
        
        if results:
            return jsonify({
                'success': True,
                'results': results,
                'poll': results  # Also return as 'poll' for compatibility
            }), 200
        else:
            return jsonify({'error': 'Poll not found'}), 404
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lectures/<lecture_id>/polls', methods=['GET'])
@api_auth_required
def get_lecture_polls(lecture_id):
    """Get all polls for a lecture"""
    try:
        polls = db.get_lecture_polls(lecture_id)
        
        return jsonify({
            'success': True,
            'polls': polls
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lectures/<lecture_id>/polls', methods=['POST'])
@api_teacher_required
def create_lecture_poll(lecture_id):
    """Create a poll for a lecture"""
    try:
        data = request.get_json()
        question = data.get('question')
        options = data.get('options')
        
        if not question or not options:
            return jsonify({'error': 'Question and options are required'}), 400
        
        if len(options) < 2:
            return jsonify({'error': 'At least 2 options are required'}), 400
        
        # Verify teacher owns this lecture
        lecture = db.get_lecture_by_id(lecture_id)
        if not lecture or lecture['teacher_id'] != session['user_id']:
            return jsonify({'error': 'Unauthorized'}), 403
        
        poll_id, message = db.create_poll(lecture_id, question, options, session['user_id'])
        
        if poll_id:
            return jsonify({
                'success': True,
                'poll_id': poll_id,
                'message': message
            }), 201
        else:
            return jsonify({'error': message}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/cohorts/<cohort_id>/polls', methods=['POST'])
@api_auth_required
def create_cohort_poll(cohort_id):
    """Create a poll for a cohort"""
    try:
        data = request.get_json()
        
        if not all(key in data for key in ['question', 'options']):
            return jsonify({'error': 'Missing required fields'}), 400
        
        # Get a lecture from the cohort to associate the poll with
        lectures = db.get_cohort_lectures(cohort_id)
        if not lectures:
            return jsonify({'error': 'No lectures found in this cohort'}), 400
        
        # Use the most recent lecture
        lecture_id = lectures[0]['id']
        
        # For admin users, use the cohort's teacher_id instead of admin user_id
        teacher_id = session['user_id']
        if session.get('user_type') == 'admin':
            cohort = db.get_cohort_by_id(cohort_id)
            if cohort:
                teacher_id = cohort['teacher_id']
        
        poll_id, response = db.create_poll(
            lecture_id, 
            data['question'], 
            data['options'],
            teacher_id
        )
        
        if poll_id:
            return jsonify({
                'success': True,
                'message': 'Cohort poll created successfully',
                'poll_id': poll_id
            }), 201
        else:
            return jsonify({'error': response}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/cohorts/<cohort_id>/polls', methods=['GET'])
@api_auth_required
def get_cohort_polls(cohort_id):
    """Get all polls for a cohort"""
    try:
        polls = db.get_cohort_polls(cohort_id)
        
        return jsonify({
            'success': True,
            'polls': polls
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/teacher/polls', methods=['GET'])
@api_teacher_required
def get_teacher_polls():
    """Get all polls created by the teacher"""
    try:
        teacher_id = session.get('user_id')
        polls = db.get_teacher_polls(teacher_id)
        
        return jsonify({
            'success': True,
            'polls': polls
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'active_sessions': len([s for s in active_sessions.values() if s['status'] == 'active']),
        'online_users': len(online_users)
    }), 200

if __name__ == '__main__':
    os.makedirs('templates', exist_ok=True)
    os.makedirs('static', exist_ok=True)
    
    if socketio:
        socketio.run(app, debug=True, host='0.0.0.0', port=5000)
    else:
        app.run(debug=True, host='0.0.0.0', port=5000)