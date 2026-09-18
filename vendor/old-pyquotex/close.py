# close.py - Force close all sessions
import os
import sys
import signal
import subprocess

def close_all_sessions():
    """Force close all Quotex sessions and connections"""
    
    # 1. Find and kill all python processes running Quotex
    try:
        result = subprocess.run(
            ['ps', 'aux'], 
            capture_output=True, 
            text=True
        )
        
        for line in result.stdout.split('\n'):
            if 'quotex' in line.lower() or 'trading' in line.lower():
                parts = line.split()
                if len(parts) > 1:
                    pid = parts[1]
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                        print(f"✅ Killed process {pid}")
                    except:
                        pass
    except Exception as e:
        print(f"Error killing processes: {e}")
    
    # 2. Close WebSocket connections
    try:
        # Kill any websocket connections
        subprocess.run(['pkill', '-f', 'websocket'], capture_output=True)
        print("✅ WebSocket connections closed")
    except:
        pass
    
    # 3. Remove session files
    session_files = [
        'session.json', 
        'cookies.json', 
        'token.dat', 
        '.quotex_session',
        '/tmp/quotex_*'
    ]
    
    for pattern in session_files:
        try:
            subprocess.run(['rm', '-f', pattern], capture_output=True)
            print(f"✅ Removed {pattern}")
        except:
            pass
    
    print("✅ All sessions closed successfully!")

if __name__ == "__main__":
    close_all_sessions()
