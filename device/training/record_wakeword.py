#!/usr/bin/env python3
"""Record real wake-word utterances on the device, through the SAME FL-channel
capture path the daemon uses (XVF3800 6ch -> ch0). Auto-segments each spoken
burst into a ~1.5-2s clip. Use these as personal positives when the synthetic
Piper voice doesn't match the speaker.

Usage (stop the daemon first to free the mic):
  systemctl --user stop hermes-voice
  python record_wakeword.py --out /tmp/ww_rec --count 50 --phrase "okay eeva"
Then scp /tmp/ww_rec/*.wav to the Mac training box and feed as positives.
"""
import os, sys, time, wave, argparse
import numpy as np, sounddevice as sd

RATE=16000; BLOCK=1280; WAKE_CH=0

def open_input():
    for ch in (6,1):
        try:
            s=sd.RawInputStream(samplerate=RATE,channels=ch,dtype="int16",blocksize=BLOCK)
            s.start(); print(f"[rec] opened {ch}ch"); return s,ch
        except Exception as e:
            print(f"[rec] {ch}ch failed: {e}")
    raise RuntimeError("no input stream")

def read_fl(s,ch):
    data,_=s.read(BLOCK)
    if ch==1: return bytes(data)
    arr=np.frombuffer(bytes(data),np.int16).reshape(-1,ch)
    return np.ascontiguousarray(arr[:,min(WAKE_CH,ch-1)]).tobytes()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--out",default="/tmp/ww_rec")
    ap.add_argument("--count",type=int,default=50)
    ap.add_argument("--phrase",default="okay eeva")
    ap.add_argument("--rms",type=float,default=600.0,help="speech RMS threshold")
    ap.add_argument("--maxlen",type=float,default=2.0,help="max clip seconds")
    ap.add_argument("--pad",type=float,default=0.3,help="pre-roll seconds")
    ap.add_argument("--silence",type=float,default=0.6,help="silence to end an utterance")
    a=ap.parse_args()
    os.makedirs(a.out,exist_ok=True)
    s,ch=open_input()
    pre=int(a.pad*RATE); ring=[]
    print(f"\n>>> Say '{a.phrase}' clearly, pausing ~1s between each. Target {a.count} clips.\n")
    n=0
    try:
        while n<a.count:
            # idle: keep a short pre-roll ring buffer until speech starts
            while True:
                fl=read_fl(s,ch); a16=np.frombuffer(fl,np.int16)
                ring.append(a16.copy());
                tot=sum(x.size for x in ring)
                while tot>pre and len(ring)>1: tot-=ring.pop(0).size
                rms=float(np.sqrt(np.mean(a16.astype(np.float32)**2))) if a16.size else 0.0
                if rms>=a.rms: break
            # capture utterance
            frames=list(ring); ring=[]; t0=time.monotonic(); last=t0
            while True:
                fl=read_fl(s,ch); a16=np.frombuffer(fl,np.int16); frames.append(a16.copy())
                rms=float(np.sqrt(np.mean(a16.astype(np.float32)**2))) if a16.size else 0.0
                now=time.monotonic()
                if rms>=a.rms: last=now
                if (now-last)>=a.silence: break
                if (now-t0)>=a.maxlen: break
            clip=np.concatenate(frames)[:int(a.maxlen*RATE)]
            n+=1
            p=os.path.join(a.out,f"{n:03d}.wav")
            w=wave.open(p,"wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes(clip.tobytes()); w.close()
            print(f"[{n}/{a.count}] saved {p}  ({clip.size/RATE:.2f}s)")
            time.sleep(0.2)
    finally:
        try: s.stop(); s.close()
        except Exception: pass
    print(f"\nDone: {n} clips in {a.out}")

if __name__=="__main__": main()
