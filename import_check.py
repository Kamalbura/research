import importlib, sys
try:
    importlib.import_module('core.config')
    importlib.import_module('tools.auto_test_gcs')
    importlib.import_module('tools.udp_echo')
    print('IMPORTS_OK')
except Exception as e:
    print('IMPORT_ERROR', e)
    sys.exit(2)
