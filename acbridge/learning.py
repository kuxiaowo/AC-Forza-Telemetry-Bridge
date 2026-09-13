"""Explicit synthetic full-load sweep; never combined with live forwarding."""
import json
import math
import os
from pathlib import Path
import re
import time
from datetime import datetime
from dataclasses import asdict
from .vehicle_curve import analyze
from .telemetry import Frame, encode


def find_car(car, config):
    if not car or Path(car).name != car or car in ('.', '..'):
        raise ValueError('尚未识别有效 AC 车辆，请先进入赛道并开始适配。')
    roots=[]
    if config.get('game_directory'):
        roots.append(Path(config['game_directory']))
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
                steam=Path(winreg.QueryValueEx(key,'SteamPath')[0])
            libraries=[steam]
            file=steam/'steamapps/libraryfolders.vdf'
            if file.exists():
                libraries += [Path(p.replace('\\\\','\\')) for p in re.findall(r'"path"\s+"([^"]+)"',file.read_text(encoding='utf-8'))]
            roots += [p/'steamapps/common/assettocorsa' for p in libraries]
        except OSError:
            pass
    roots += [Path(f'{drive}:/Steam/steamapps/common/assettocorsa') for drive in 'CDEFGH']
    candidates=list(dict.fromkeys((p/'content/cars'/car).resolve() for p in roots if (p/'content/cars'/car).is_dir()))
    if config.get('game_directory') and candidates and candidates[0]==(roots[0]/'content/cars'/car).resolve():
        return candidates[0]
    if len(candidates)!=1:
        raise ValueError('无法唯一匹配游戏安装目录，请在“游戏目录”中选择正在使用的 AC 根目录。')
    return candidates[0]


def curve_matches(actual, expected):
    if len(actual)<20:
        return False
    points=sorted(actual,key=lambda p:p.get('Rpm',0))
    if points[0]['Rpm']>expected[0]['Rpm']+2 or points[-1]['Rpm']<expected[-1]['Rpm']-2:
        return False
    if any(b['Rpm']-a['Rpm']>300 for a,b in zip(points,points[1:])):
        return False
    for point in expected:
        nearest=min(points,key=lambda p:abs(p['Rpm']-point['Rpm']))
        if abs(nearest['Rpm']-point['Rpm'])>2 or abs(nearest['PowerWatts']-point['PowerWatts'])>max(2,point['PowerWatts']*.01):
            return False
    return True


def sweep_frames(profile, car):
    points=profile['PowerCurvePoints']
    # 50 RPM every 6 packets at 60 Hz = 500 RPM/s. Allow gear cooldown first.
    def frame(point, throttle=1):
        return Frame(active=True,car=car,track='文件曲线模拟学习（非实车）',status='动力学习 · 模拟数据',
            rpm=point['Rpm'],max_rpm=profile['EngineMaxRpm'],gear=3,throttle=throttle,
            speed_kmh=36+point['Rpm']*.015,normalized_suspension=(.5,)*4,
            power_watts=point['PowerWatts'],torque_nm=point['TorqueNm'])
    for _ in range(90):yield frame(points[0],0)
    for point in points:
        for _ in range(6):yield frame(point)
    # Let the redline observer age the peak, then emulate limiter decline.
    for _ in range(15):yield frame(points[-1])
    dropped=dict(points[-1],Rpm=points[-1]['Rpm']-90)
    dropped['PowerWatts']=dropped['TorqueNm']*dropped['Rpm']*math.pi/30
    for _ in range(15):yield frame(dropped)
    for _ in range(30):yield frame(points[-1],0)
    for _ in range(15):yield Frame(car=car,max_rpm=profile['EngineMaxRpm'])


def run_learning(bridge,sock,target,live_frame):
    base=bridge.base
    car=live_frame.car
    directory=find_car(car,bridge.config)
    profile,info=analyze(directory,bridge.config.get('curve_data_source','auto'),live_frame.max_rpm)
    record_dir=base/'learning_records'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    record_dir.mkdir(parents=True)
    info.update(car_directory=str(directory),kind='synthetic_udp_learning',verified=False)
    record=record_dir/'result.json'
    record.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    frames=list(sweep_frames(profile,car))
    deadline=time.monotonic()
    sent=0
    try:
        for i,frame in enumerate(frames):
            if bridge.stop_event.is_set():raise ValueError('学习已取消；原程序可能保留部分曲线。')
            if i%60==0:
                # Read live identity even while forwarding is suspended.
                from .shared_memory import utf16
                _,_,static=bridge.reader.read()
                if utf16(static.carModel)!=car:raise ValueError('游戏车辆已改变，取消本次学习。')
            sock.sendto(encode(frame,int(time.monotonic()*1000),bridge.config['packet_format']),target)
            sent+=1
            bridge.publish(status=f'模拟动力学习 {i*100//len(frames)}% · {car} · 真实转发已暂停',frame=asdict(frame))
            deadline+=1/60
            if deadline<time.monotonic():deadline=time.monotonic()
            bridge.stop_event.wait(max(0,deadline-time.monotonic()))
        info['transmission_complete']=True
        return '完整模拟扫描发送完成，真实转发已恢复（不检测仪表盘，不确认接收或保存）'
    finally:
        for _ in range(3):sock.sendto(encode(Frame(car=car),int(time.monotonic()*1000),bridge.config['packet_format']),target)
        info['sent_packets']=sent
        record.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')

