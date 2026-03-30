def bad_function():
    try:
        data = fetch_secure_data()
    except Exception:
        pass # Netflix rule violation

def fetch_secure_data():
    return "Sensitive Information"
