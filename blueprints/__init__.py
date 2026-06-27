from flask import Blueprint

main_bp = Blueprint('main', __name__)
admin_bp = Blueprint('admin', __name__)
api_bp = Blueprint('api', __name__)

# Import the modules to register the routes on the blueprints
# This must happen when the package is imported
from . import main as _main
from . import admin as _admin
from . import api as _api