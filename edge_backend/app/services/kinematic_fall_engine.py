import time
from typing import List, Dict, Any, Optional, Tuple
from ..models.schemas import EventType, EventSeverity, SecurityEvent

class KinematicFallEngine:
    def __init__(self):
        self._history: Dict[str, Dict[int, List[Dict[str, Any]]]] = {}
        self._immobility_timers: Dict[str, Dict[int, float]] = {}

    def evaluate_pose(
        self, 
        camera_id: str, 
        camera_name: str,
        track_id: int, 
        keypoints: List[Tuple[float, float, float]],
        bbox: Tuple[float, float, float, float]
    ) -> Optional[SecurityEvent]:
        now = time.time()
        
        if len(keypoints) < 17:
            return None
            
        x1, y1, x2, y2 = bbox
        width = max(1.0, x2 - x1)
        height = max(1.0, y2 - y1)
        aspect_ratio = height / width
        
        l_hip, r_hip = keypoints[11], keypoints[12]
        if l_hip[2] > 0.3 and r_hip[2] > 0.3:
            com_y = (l_hip[1] + r_hip[1]) / 2.0
            com_x = (l_hip[0] + r_hip[0]) / 2.0
        else:
            com_y = (y1 + y2) / 2.0
            com_x = (x1 + x2) / 2.0

        if camera_id not in self._history:
            self._history[camera_id] = {}
        if track_id not in self._history[camera_id]:
            self._history[camera_id][track_id] = []
            
        history = self._history[camera_id][track_id]
        history.append({
            "timestamp": now,
            "com_y": com_y,
            "com_x": com_x,
            "aspect_ratio": aspect_ratio,
            "bbox_height": height
        })
        
        history = [h for h in history if now - h["timestamp"] <= 4.0]
        self._history[camera_id][track_id] = history
        
        if len(history) < 2:
            return None
            
        dt = history[-1]["timestamp"] - history[0]["timestamp"]
        if dt > 0.05:
            dy = history[-1]["com_y"] - history[0]["com_y"]
            velocity = dy / dt
            
            initial_ar = history[0]["aspect_ratio"]
            current_ar = history[-1]["aspect_ratio"]
            
            is_rapid_fall = (velocity > 180.0 and initial_ar > 1.2 and current_ar < 0.9)
            
            if is_rapid_fall:
                if camera_id not in self._immobility_timers:
                    self._immobility_timers[camera_id] = {}
                self._immobility_timers[camera_id][track_id] = now
                
                return SecurityEvent(
                    id=f"evt_fall_{int(now*1000)}",
                    camera_id=camera_id,
                    camera_name=camera_name,
                    event_type=EventType.FALL_DETECTED,
                    severity=EventSeverity.CRITICAL,
                    timestamp=now,
                    confidence=0.94,
                    description=f"Rapid fall impact detected on Track #{track_id} (Velocity: {velocity:.1f}px/s).",
                    metadata={"track_id": track_id, "velocity": velocity, "aspect_ratio": current_ar}
                )
                
        if camera_id in self._immobility_timers and track_id in self._immobility_timers[camera_id]:
            fall_time = self._immobility_timers[camera_id][track_id]
            if (now - fall_time) >= 10.0 and aspect_ratio < 0.9:
                del self._immobility_timers[camera_id][track_id]
                return SecurityEvent(
                    id=f"evt_immobility_{int(now*1000)}",
                    camera_id=camera_id,
                    camera_name=camera_name,
                    event_type=EventType.DANGER_ZONE_IMMOBILITY,
                    severity=EventSeverity.CRITICAL,
                    timestamp=now,
                    confidence=0.98,
                    description=f"Unresponsive floor immobility verified (> 10s post-fall).",
                    metadata={"track_id": track_id, "immobility_duration_s": round(now - fall_time, 1)}
                )

        return None

kinematic_fall_engine = KinematicFallEngine()
