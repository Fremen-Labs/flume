def run_worker():
    from handlers.pm import handle_pm_dispatcher_worker
    print(handle_pm_dispatcher_worker())

def helper():
    return "helper"

if __name__ == '__main__':
    run_worker()
