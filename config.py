import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Supabase configuration
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
    
    # Application configuration
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024
    
    # WebRTC and SocketIO configuration
    SOCKETIO_CORS_ALLOWED_ORIGINS = "*"
    SOCKETIO_ASYNC_MODE = 'threading'
    
    # Upload folders
    UPLOAD_FOLDER = 'uploads'
    COMPRESSED_FOLDER = 'compressed'
    
    # Allowed extensions
    ALLOWED_AUDIO_EXTENSIONS = {'mp3', 'wav', 'm4a', 'ogg'}
    ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
    ALLOWED_DOCUMENT_EXTENSIONS = {'pdf', 'txt', 'doc', 'docx', 'ppt', 'pptx'}
    
    # Compression settings
    AUDIO_BITRATE = '48k'
    IMAGE_QUALITY = 30
    PDF_COMPRESSION_LEVEL = 3
    
    # Babel configuration
    LANGUAGES = {
        'en': 'English',
        'hi': 'हिन्दी'
    }
    BABEL_DEFAULT_LOCALE = 'en'
    BABEL_DEFAULT_TIMEZONE = 'UTC'
    BABEL_TRANSLATION_DIRECTORIES = 'translations'