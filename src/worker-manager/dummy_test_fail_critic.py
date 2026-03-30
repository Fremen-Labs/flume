def bad_function():
    try:
        fetch_secure_data()
    except Exception:
        pass # Netflix rule violation

def fetch_secure_data():
    return "Sensitive Information"
