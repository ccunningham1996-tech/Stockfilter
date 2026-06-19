import os
import time
import subprocess
import sys
from datetime import datetime, timedelta

def run_job(script_name):
    print(f"[{datetime.now()}] Starting {script_name}...")
    try:
        # Run using the same python interpreter as the scheduler
        result = subprocess.run([sys.executable, script_name], capture_output=True, text=True, check=True)
        print(f"[{datetime.now()}] Finished {script_name}.")
        print("STDOUT:")
        print(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"[{datetime.now()}] ERROR running {script_name}: {e}")
        print("STDOUT:")
        print(e.stdout)
        print("STDERR:")
        print(e.stderr)

def run_pipeline():
    print(f"=== Pipeline execution started at {datetime.now()} ===")
    
    # 1. Check active trade exits (sell first to free up cash)
    run_job("src/outcome_tracker.py")
    
    # 2. Scrape today's upgrades
    run_job("src/scraper.py")
    
    # 3. Filter and buy new upgrades using reinvested cash
    run_job("src/filter.py")
    
    print(f"=== Pipeline execution finished at {datetime.now()} ===\n")

def get_eastern_time():
    # UTC time
    utc_now = datetime.utcnow()
    # EST is UTC-5, EDT is UTC-4. Calculate based on standard US DST rules.
    # EDT starts second Sunday of March, ends first Sunday of November.
    year = utc_now.year
    # Find second Sunday of March
    march_1 = datetime(year, 3, 1)
    march_14 = datetime(year, 3, 14)
    dst_start = march_14 - timedelta(days=(march_14.weekday() + 1) % 7)
    
    # Find first Sunday of November
    nov_1 = datetime(year, 11, 1)
    dst_end = nov_1 - timedelta(days=(nov_1.weekday() + 1) % 7)
    
    if dst_start <= utc_now < dst_end:
        offset = -4  # EDT
    else:
        offset = -5  # EST
        
    return utc_now + timedelta(hours=offset)

def main():
    print(f"Scheduler started. Current Eastern Time: {get_eastern_time()}")
    print("Will run the pipeline every weekday (Mon-Fri) at 16:15 Eastern Time.")
    
    last_run_date = None
    
    while True:
        try:
            now_eastern = get_eastern_time()
            current_date = now_eastern.date()
            weekday = now_eastern.weekday() # 0 = Monday, 6 = Sunday
            
            # Run only on weekdays (Monday=0 to Friday=4)
            if weekday < 5:
                # Check if it's 16:15 or later, and we haven't run today yet
                if now_eastern.hour == 16 and now_eastern.minute >= 15 and last_run_date != current_date:
                    run_pipeline()
                    last_run_date = current_date
            
            # Sleep 30 seconds before checking again
            time.sleep(30)
        except KeyboardInterrupt:
            print("Scheduler stopped by user.")
            break
        except Exception as e:
            print(f"Scheduler error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    # If run with --now, run immediately once, then exit
    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        run_pipeline()
    else:
        main()
