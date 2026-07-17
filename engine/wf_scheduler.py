WARMUP = 60; TRAIN = 400; PURGE = 35; TEST = 80; STEP = 50
TOTAL_WINDOW = WARMUP + TRAIN + PURGE + TEST

def generate_windows(n_days):
    windows = []
    start = 0
    while start + TOTAL_WINDOW <= n_days:
        w1 = start + WARMUP
        w2 = w1 + TRAIN
        w3 = w2 + PURGE
        w4 = w3 + TEST
        windows.append({"idx":len(windows),"warmup":(start,w1),"train":(w1,w2),"test":(w3,w4)})
        start += STEP
    return windows

def verify_no_leakage(windows):
    for w in windows:
        assert w["train"][1] + PURGE == w["test"][0], f"leak in window {w['idx']}"
