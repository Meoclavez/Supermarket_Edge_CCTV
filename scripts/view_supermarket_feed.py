#!/usr/bin/env python3
"""
Pearcedale Supermarket CCTV Live Stream Viewer & Probe Utility
Project: Edge AI CCTV Surveillance Platform
Location: Pearcedale, Victoria 3912, Australia
"""

import argparse
import os
import sys
import time
import subprocess

# Auto-switch to virtual environment if cv2 is not in current interpreter
try:
    import cv2
    import numpy as np
except ImportError:
    venv_python = os.path.expanduser("~/.venv/bin/python")
    if os.path.exists(venv_python) and sys.executable != venv_python:
        os.execv(venv_python, [venv_python] + sys.argv)
    cv2 = None
    np = None

DEFAULT_SERIAL = "8L0D384PAZ1EBB0"
DEFAULT_USER = "admin"
DEFAULT_PWDS = ["Pearcedale3912", "Pearcedale1"]
DEFAULT_LOCAL_IP = "192.168.20.160"
DEFAULT_RTSP_PORT = 554

def check_ffplay_available():
    return subprocess.call("which ffplay > /dev/null 2>&1", shell=True) == 0

def test_rtsp_stream(rtsp_url, timeout_sec=5):
    """Attempt to read frames from RTSP stream using OpenCV."""
    print(f"[*] Testing RTSP Stream: {rtsp_url}")
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    
    start_time = time.time()
    success = False
    frame = None
    
    while time.time() - start_time < timeout_sec:
        ret, f = cap.read()
        if ret and f is not None:
            success = True
            frame = f
            break
        time.sleep(0.1)
        
    cap.release()
    return success, frame

def view_live_feed(rtsp_url, window_title="Supermarket CCTV Live Stream"):
    """Open full interactive OpenCV display window with HUD."""
    print(f"\n[+] Opening Live Stream: {rtsp_url}")
    print("[*] Press 'q' or ESC in the video window to exit.")
    
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print(f"[-] Failed to open stream at {rtsp_url}")
        return False
        
    cv2.namedWindow(window_title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_title, 1280, 720)
    
    fps_history = []
    prev_time = time.time()
    
    try:
        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                print("[-] Warning: Frame dropped or connection paused. Reconnecting...")
                time.sleep(0.2)
                continue
                
            curr_time = time.time()
            dt = curr_time - prev_time
            prev_time = curr_time
            if dt > 0:
                fps_history.append(1.0 / dt)
                if len(fps_history) > 30:
                    fps_history.pop(0)
            avg_fps = sum(fps_history) / len(fps_history) if fps_history else 0.0
            
            # Draw HUD Overlay
            h, w = frame.shape[:2]
            overlay = frame.copy()
            cv2.rectangle(overlay, (10, 10), (450, 75), (20, 20, 20), -1)
            cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)
            
            cv2.putText(frame, "Pearcedale Supermarket Live Feed", (20, 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 200), 2, cv2.LINE_AA)
            cv2.putText(frame, f"Stream: {w}x{h} | FPS: {avg_fps:.1f} | Live P2P/RTSP", (20, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
            
            cv2.imshow(window_title, frame)
            
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q') or key == 27:
                break
            elif key == ord('s'):
                snap_path = f"snapshot_{int(time.time())}.jpg"
                cv2.imwrite(snap_path, frame)
                print(f"[+] Saved snapshot to {snap_path}")
    finally:
        cap.release()
        cv2.destroyAllWindows()
    return True

def main():
    parser = argparse.ArgumentParser(description="Pearcedale Supermarket CCTV Stream Viewer")
    parser.add_argument("--host", default=DEFAULT_LOCAL_IP, help="Target Host / IP (default: 192.168.20.160)")
    parser.add_argument("--port", type=int, default=DEFAULT_RTSP_PORT, help="RTSP Port (default: 554)")
    parser.add_argument("--channel", type=int, default=1, help="Camera Channel 1-16 (default: 1)")
    parser.add_argument("--subtype", type=int, default=1, choices=[0, 1], help="0=Main Stream (HD), 1=Sub Stream (Fast/AI)")
    parser.add_argument("--user", default=DEFAULT_USER, help="Username (default: admin)")
    parser.add_argument("--password", default=DEFAULT_PWDS[0], help="Password (default: Pearcedale3912)")
    parser.add_argument("--p2p", action="store_true", help="Launch via local Dahua P2P bridge tunnel")
    parser.add_argument("--ffplay", action="store_true", help="Launch via FFplay instead of OpenCV")
    args = parser.parse_args()

    host = args.host
    port = args.port
    
    if args.p2p:
        host = "127.0.0.1"
        port = 8554
        print(f"[*] Connecting via Dahua P2P Bridge on {host}:{port} for SN: {DEFAULT_SERIAL}")

    rtsp_url = f"rtsp://{args.user}:{args.password}@{host}:{port}/cam/realmonitor?channel={args.channel}&subtype={args.subtype}"
    print("=" * 60)
    print("  PEARCEDALE SUPERMARKET CCTV - FEED VIEWER")
    print("=" * 60)
    print(f"  Target Host  : {host}:{port}")
    print(f"  Channel      : {args.channel} ({'Sub-stream' if args.subtype==1 else 'Main-stream'})")
    print(f"  Username     : {args.user}")
    print(f"  RTSP URL     : {rtsp_url}")
    print("=" * 60)

    if args.ffplay:
        if not check_ffplay_available():
            print("[-] ffplay not found on system PATH. Falling back to OpenCV viewer.")
        else:
            cmd = f'ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay -i "{rtsp_url}"'
            print(f"[*] Launching FFplay:\n{cmd}")
            os.system(cmd)
            return

    # Attempt direct stream opening
    success = view_live_feed(rtsp_url, f"Pearcedale Supermarket - Camera {args.channel}")
    if not success:
        print("\n[!] Could not connect to direct RTSP endpoint.")
        print("[*] Alternative viewing options:")
        print(f" 1. Mobile App: Open DMSS -> Add SN '{DEFAULT_SERIAL}' -> Password '{args.password}'")
        print(f" 2. Public IP : Run with --host <AUSTRALIAN_PUBLIC_IP>")
        print(f" 3. Test other password: --password {DEFAULT_PWDS[1]}")

if __name__ == "__main__":
    main()
