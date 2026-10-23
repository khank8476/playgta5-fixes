"""Patch game.wasm so shop menus open although the 2014 data set lacks NG/MP menu assets.

The shop scripts wait for MPShopSale.ytd and the COLOUR_SWITCHER_01/02 scaleform movies,
which this data set (no update.rpf) does not contain, so the menu never opens.
- HAS_STREAMED_TEXTURE_DICT_LOADED: a dictionary that is not in the store reports loaded.
- REQUEST_SCALEFORM_MOVIE: a plain movie name that is not in the scaleform store returns MISSING_MOVIE (-50)
  before CreateMovie runs; CreateMovie would add a "non_str_<name>" slot for it that never loads.
  Every movie command rejects ids <= 0, and HAS_SCALEFORM_MOVIE_LOADED(MISSING_MOVIE) reports loaded
  (patched in the command and in the script native, which inlines its own copy).
  Id 0 keeps meaning "not requested", so scripts still request movies they have not asked for yet.
  Names with '/', '\\' or '.' skip the check: CreateMovie loads those from a path, not from the store.

Also a cash hotkey: each press of the key index.html maps to CASH_KEY_FLAG adds $999,999 to the current
character's SPn_TOTAL_CASH, through the same calls a money pickup makes (CPickupRewardMoneyFixed::Give).

Also a resource heap cap: sysMemGrowBuddyAllocator::GrowHeap adds a 256 MB heap whenever the free space is below 4 MB,
up to 32 heaps, so the streaming cache never evicts and grows until the 3 GB wasm memory is gone (a session crashed
with "Invalid fixup" at 1256/1280 MB). With a heap limit, GetMemoryAvailable reports the real free
space and strStreamingAllocator::MakeSpaceForAllocation deletes the least used objects instead.
The limit follows the machine's RAM (heap_limit); --heaps N overrides it. 4 heaps (1 GB) stalled missions.
Two fixes make the cap safe on big map files. After a failed allocation, strStreamingLoader::LoadRequestedObjects
marked the request as fitting and read it into chunks that were never allocated (pgStreamer::Read trap); it now
waits for a later frame. Archives marked loadCompletely in images.meta (the hills' *_instance_placement.rpf, 13-57 MB)
needed one free block of their full size, which a capped heap rarely has ("Failed to allocation virtual memory of
size 57360384"); strPackfileManager::AddImageToList now reads them entry by entry like the other archives.

Also a log flush fix: netLogImpl::Flush(true) loops until the log spool thread has written the pending buffer.
CNetwork::Shutdown stops that thread on every session shutdown (in-game Load Game, the return after Replay Mission),
and CAILogManager::ShutDown flushes its logs after that, so the main thread spun forever right after "shutdown!".
The wait now ends when the spool thread is gone; the flush then writes what it can without it.
netLogImpl::HandleBufferSwapFailure (a log line that does not fit) has the same wait in a retry loop; it now returns
when the spool thread is gone, and the caller drops that line.

Also a Snapmatic photo fix. fiDeviceMemory::Create read the buffer address of a "memory:$%p,..." file name from
name + 8, but musl's %p writes "0x" first, so the address read as 0 and every write into that file failed. The phone
camera writes its JPEG this way, and libjpeg's write error ended the game. Create now skips the "0x" like Open does.
The photo was also black: wgpu_worker.js skips a draw while its pipeline still compiles, and the one quad that copies
the frame into the photo had no recipe in shaders/pipelines.json, so it was never compiled ahead. The script adds it
to pipelines.json and pipelines_low.json.

Also a script camera fix. With keyboard and mouse, Paparazzo's film camera reads only INPUT_SCALED_LOOK_LR/UD, and the
2013 control data (data/common/data/control/settings.meta) has no axis for them, so they stay 0 and the camera does not
turn. The script adds both axes, built from the same halves as INPUT_LOOK_LR/UD, so they get the same mouse and pad input.
io_worker.js reads only the manifest size of the file, so it keeps its size: its CRLF line ends become LF and spaces fill
the end. The untouched file is kept as settings.meta.orig.

Also a debug text fix. The "Allow script debug draw" toggle starts on in this build, so scripts draw their debug text,
such as the blue "runVendingMachine" near vending machines. Only the script debug draw commands read it, in 21 checks of
the form i64.const 6643884; i32.load8_u; i32.const 1; i32.ne; br_if 0. Each load becomes i32.const 0 and nops.

Reads b/8b0b5899ed/game.wasm.orig (the untouched backup) and writes b/8b0b5899ed/game.wasm.
On the first run game.wasm is the untouched file: it is checked and copied to game.wasm.orig first.
Restore: cp game.wasm.orig game.wasm
"""
import argparse, hashlib, json, os, shutil, struct, sys
from pathlib import Path

DIR = Path(__file__).resolve().parent / 'mirror' / 'playgta5.com' / 'b' / '8b0b5899ed'
ORIG_SHA256 = '11ca8d2c04c5e843d18ff4aea4899d72c86973c6b031df334e67c446b2ae83e0'

# Body file offsets (wasm-objdump) of the functions, checked against their indices below.
HAS_TXD_LOADED = (50534, 0x1dc1382)          # graphics_commands::HasStreamedTxdLoaded
REQUEST_MOVIE = (50785, 0x1dc8e5f)           # graphics_commands::CommandRequestScaleformMovieCommon
HAS_MOVIE_LOADED = (50789, 0x1dc92c4)        # graphics_commands::CommandHasScaleformMovieLoaded
HAS_MOVIE_LOADED_NATIVE = (51266, 0x1dd6879) # scrWrapped_HAS_SCALEFORM_MOVIE_LOADED::Call (inlines the command)
STATS_UPDATE = (77092, 0x2647586)            # CStatsMgr::Update, once per game frame
GROW_HEAP = (3036, 0x264d27)                 # rage::sysMemGrowBuddyAllocator::GrowHeap
LOAD_REQUESTED = (6213, 0x388049)            # rage::strStreamingLoader::LoadRequestedObjects
ADD_IMAGE = (6049, 0x372a26)                 # rage::strPackfileManager::AddImageToList
MEMORY_CREATE = (1822, 0x1ea8d9)             # rage::fiDeviceMemory::Create
# call SetExtraMemory; end; i32.const 1 (fits); local.set 6; end; ...; local.get 6; br_if 1
NO_ROOM_TEST = bytes.fromhex('10fb4d 0b 4101 2106 0b 2029 42f0007c 2400 2006 0d01')
# local.get 0; i32.const 256 (loadCompletely); i32.const 0; local.get 3; select; i32.const 8; ... i32.or
WHOLE_LOAD_FLAG = bytes.fromhex('2000 418002 4100 2003 1b 4108 4100 2001 1b 72')
# Each has an inlined CMovieMgr::CMovie::WaitTillLoaded: unless flags97 & 1, sleep until bwMovie byte 171 (loaded) is set.
# Only the AsyncBink thread sets it, and the wasm build never starts that thread (no Bink), so the main thread sleeps
# forever once a TV movie is deleted (Michael's TV in Armenian 3). 'and' -> 'or' makes the skip test always true.
MOVIE_WAITS = ((80969, 0x29996d6), (80987, 0x29a3c98), (81493, 0x29fbf85), (81494, 0x29fc348), (81507, 0x29fd2b2),
               (36164, 0x14905f6), (36283, 0x14af1e9))  # CMovieMgr: UpdateFrame, Delete, CMovie::Play, Stop, GetTime; CPauseMenu: Open, SetupCodeForUnPause
MOVIE_WAIT_TEST = bytes.fromhex('2d0061' '4101' '71' '45' '0440' '4285f004')  # load8_u 97; i32.const 1; and; eqz; if; i64.const 79877
# strncmp(name, "memory:", 7); br_if; local.get 1; i64.const 8 (name + 8, the byte that becomes 10); i64.add; local.set 0; loop
MEMORY_NAME_TEST = bytes.fromhex('2001 42c48eb901 4207 10bec605 0d00 2001 4208 7c 2100 0340')

# The pipeline that copies the frame into the photo (PS_Blit into the rgba8unorm photo target), as wgpu_worker.js records it.
SETTINGS = DIR.parent.parent / 'data' / 'common' / 'data' / 'control' / 'settings.meta'
SETTINGS_SHA256 = 'a0e165a0907bef6d377b253e4465d4b1a952808a47795a303530199fa9bfd8e5'
SCALED_LOOK_AXES = (b'<Item><Input>INPUT_SCALED_LOOK_UD</Input><Negative>INPUT_LOOK_UP_ONLY</Negative><Positive>INPUT_LOOK_DOWN_ONLY</Positive></Item>'
                    b'<Item><Input>INPUT_SCALED_LOOK_LR</Input><Negative>INPUT_LOOK_LEFT_ONLY</Negative><Positive>INPUT_LOOK_RIGHT_ONLY</Positive></Item>')
PHOTO_PIPELINE = {"vs": "4e1d5d361e0ed0bc", "ps": "01a05b914ee571f0",
                  "el": [[2013849738, 0, 6, 0, 0, 0, 0], [1009949074, 0, 6, 0, 12, 0, 0], [3888318712, 0, 28, 0, 24, 0, 0], [197415955, 0, 16, 0, 28, 0, 0]],
                  "st": [36], "tp": 4, "sf": "", "b": [0, 0, 0, 2, 1, 1, 2, 1, 1, 15] + [0, 2, 1, 1, 2, 1, 1, 15] * 7,
                  "d": [0, 0, 8, 0, 65535, 1, 1, 1, 8, 1, 1, 1, 8], "r": [3, 1, 1, 0, 0, 0, 1, 1, 0, 0],
                  "rt": ["rgba8unorm"], "n": 1, "ds": "", "ro": 0}

# Cash hotkey. index.html sets CASH_KEY_FLAG once per press; CStatsMgr::Update consumes it.
CASH_KEY_FLAG = 6583436 + 0xE8   # in the engine's 256-byte keyboard block (wasm_input_test_key); VK 0xE8 is unassigned
CASH_STAT = 25469204             # StatId_char {SP0,SP1,SP2}_TOTAL_CASH (+ MP slots), set up in __wasm_call_ctors
CASH_AMOUNT = 999999.0           # IncrementStat takes a float; exact below 2**24
GET_STAT_ID, IS_KEY_VALID, INCREMENT_STAT = 77153, 77193, 77089  # StatId_char::GetStatId, CStatsDataMgr::IsKeyValid, CStatsMgr::IncrementStat

LOG_FLUSH = (8042, 0x48c0c7)     # rage::netLogImpl::Flush(bool)
LOG_SWAP_FAILURE = (8040, 0x48be2e)  # rage::netLogImpl::HandleBufferSwapFailure()
SPOOL_THREAD_ID = 6630952        # rage::LogSpoolFunction's thread id; CNetwork::Shutdown sets it to -1 after the thread exits
FLUSH_WAIT_LOOP = bytes.fromhex('2000 4208 7c 2102 0340')  # local.get 0; i64.const 8; i64.add; local.set 2; loop
SWAP_WAIT_LOOP = bytes.fromhex('0440 0340')  # if (pending) loop: the wait inside the swap retry loop (the only if-loop there)

DEBUG_DRAW_CHECK = bytes.fromhex('42acc19503 2d0000 4101 47 0d00')  # i64.const 6643884; i32.load8_u; i32.const 1; i32.ne; br_if 0
DEBUG_DRAW_OFF = bytes.fromhex('4100 010101010101')  # i32.const 0; 6 nops


def asm(text):
    """Hex bytes, one instruction group per line; '#' starts a comment."""
    return bytes.fromhex(''.join(line.split('#')[0] for line in text.splitlines()).replace(' ', ''))


# HasStreamedTxdLoaded entry: the result (local 2) starts at 1, which every not-found path keeps.
TXD_ENTRY = asm('4101 2102')

# CommandRequestScaleformMovieCommon entry, before its stack frame. Locals 7, 8: i32, 9: i64 (new).
# The store's name map, as CScaleformMgr::CreateMovieInternal reads it: i32 size at 19601576, bucket
# array pointer at 19601568, entry array pointer at 19601560; entries are {hash, slot, next}, -1 ends a chain.
MOVIE_PRECHECK = asm('''
    0240                         # block $known
    2000 2109                    #   p = name
    0240 0340                    #   block, loop: scan the name
    2009 2d0000 2208 45 0d01     #     c = *p; end of string -> scan done
    2008 412f 46 0d02            #     '/' -> $known
    2008 412e 46 0d02            #     '.' -> $known
    2008 41dc00 46 0d02          #     '\\' -> $known
    2009 4201 7c 2109 0c00       #     p++
    0b 0b
    4101 2000 10a805 2107        #   hash = ComputeHash(1, name)   (func 680)
    42a8b1ac09 280200 2208       #   n = map size
    45 0d00                      #   empty map (store not set up) -> $known
    42a0b1ac09 290300 2007 2008 70 ad 4202 86 7c 280200 2108    # i = buckets[hash % n]
    0240 0340                    #   block, loop over the chain
    2008 417f 46 0d01            #     i == -1 -> not found
    4298b1ac09 290300 2008 ad 420c 7e 7c 2209                   # e = &entries[i]
    280200 2007 46 0440          #     if (e.hash == hash)
    2009 280204 417f 47 0d03     #       e.slot != -1 -> $known
    0c02 0b                      #       else not found
    2009 280208 2108 0c00        #     i = e.next
    0b 0b
    414e 0f                      #   return MISSING_MOVIE (-50)
    0b
''')
# CommandHasScaleformMovieLoaded entry: if (id == MISSING_MOVIE) return 1;
MOVIE_LOADED_CHECK = asm('2000 414e 46 0440 4101 0f 0b')
# The script native has its own inlined copy: if (args[0] == MISSING_MOVIE) { *result = 1; return; }
MOVIE_LOADED_NATIVE_CHECK = asm('''
    2000 290310 280200 414e 46 0440    # info.args[0] == MISSING_MOVIE
    2000 290300 4101 360200 0f         #   *info.result = 1; return
    0b
''')


def leb(data, i):
    result = shift = 0
    while True:
        byte = data[i]; i += 1
        result |= (byte & 0x7f) << shift; shift += 7
        if byte < 0x80:
            return result, i


def enc_leb(value):
    out = bytearray()
    while True:
        byte = value & 0x7f; value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def sleb(value):
    out = bytearray()
    while True:
        byte = value & 0x7f; value >>= 7
        if (value == 0 and not byte & 0x40) or (value == -1 and byte & 0x40):
            return bytes(out + bytes([byte]))
        out.append(byte | 0x80)


def cash_hotkey():
    """CStatsMgr::Update entry: if (flag) { flag = 0; id = CASH.GetStatId(); if (IsKeyValid(id)) IncrementStat(id, amount, 3); }"""
    i64 = lambda v: (b'\x42' + sleb(v)).hex()
    call = lambda f: (b'\x10' + enc_leb(f)).hex()
    return asm(f'''
        {i64(CASH_KEY_FLAG)} 2d0000 0440                       # if (flag)
        {i64(CASH_KEY_FLAG)} 4100 3a0000                       #   flag = 0
        2300 4210 7d 2400                                      #   sp -= 16
        2300 4208 7c {i64(CASH_STAT)} {call(GET_STAT_ID)}      #   id (at sp + 8) = the current character's cash stat
        2300 4208 7c {call(IS_KEY_VALID)} 0440                 #   if (IsKeyValid(id))
        2300 4208 7c 43{struct.pack('<f', CASH_AMOUNT).hex()} 4103 {call(INCREMENT_STAT)}
        0b
        2300 4210 7c 2400                                      #   sp += 16
        0b
    ''')


def locals_end(body):
    count, i = leb(body, 0)
    for _ in range(count):
        _, i = leb(body, i)
        i += 1
    return i


def check(ok, what):
    if not ok:
        sys.exit(f'{what} does not match; not the expected game.wasm, nothing written')


def ram_gb():
    try:
        if sys.platform == 'win32':
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_uint32), ('load', ctypes.c_uint32), ('total', ctypes.c_uint64),
                            ('rest', ctypes.c_uint64 * 6)]
            status = MemoryStatus(ctypes.sizeof(MemoryStatus))
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return status.total / 2**30 or None
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 2**30
    except (AttributeError, OSError, ValueError):
        return None


def heap_limit(ram):
    """Heaps of 256 MB for the streaming cache. 6 (1.5 GB) ran Armenian1 at 1529/1536 MB with one 0.4 s hitch.
    8 is not played yet (the tab would reach about 3.9 GB), so it is only available through --heaps."""
    return 6 if ram is not None and ram < 12 else 7


def patch(data, heaps):
    i = 8
    while data[i] != 10:
        size, j = leb(data, i + 1)
        i = j + size
    section_start = i
    section_size, j = leb(data, i + 1)
    section_end = j + section_size
    count, p = leb(data, j)
    bodies, starts = [], {}
    while p < section_end:
        size, q = leb(data, p)
        starts[q] = len(bodies)
        bodies.append(bytearray(data[q:q + size]))
        p = q + size
    check(len(bodies) == count and HAS_TXD_LOADED[1] in starts, 'code section')

    first_index = HAS_TXD_LOADED[0] - starts[HAS_TXD_LOADED[1]]
    for index, offset in (REQUEST_MOVIE, HAS_MOVIE_LOADED, HAS_MOVIE_LOADED_NATIVE, STATS_UPDATE, GROW_HEAP,
                          LOAD_REQUESTED, ADD_IMAGE, LOG_FLUSH, LOG_SWAP_FAILURE, MEMORY_CREATE):
        check(starts.get(offset, -1) + first_index == index, f'func[{index}] offset')
    body = lambda fn: bodies[fn[0] - first_index]

    txd = body(HAS_TXD_LOADED)
    check(txd[:5] == bytes.fromhex('02037f017e'), 'HasStreamedTxdLoaded locals')
    check(txd[5:14] == bytes.fromhex('41004100200010a805'), 'HasStreamedTxdLoaded entry')
    txd[5:5] = TXD_ENTRY

    req = body(REQUEST_MOVIE)
    check(req[:5] == bytes.fromhex('02037e017f'), 'CommandRequestScaleformMovieCommon locals')
    check(req[5:15] == bytes.fromhex('230042a0017d22032400'), 'CommandRequestScaleformMovieCommon frame')
    req[:5] = bytes.fromhex('03037e037f017e') + MOVIE_PRECHECK  # + locals 7, 8: i32, 9: i64

    has = body(HAS_MOVIE_LOADED); at = locals_end(has)
    check(has[at:at + 7] == bytes.fromhex('0240200041016b'), 'HasScaleformMovieLoaded entry')
    has[at:at] = MOVIE_LOADED_CHECK

    native = body(HAS_MOVIE_LOADED_NATIVE); at = locals_end(native)
    check(native[at:at + 8] == bytes.fromhex('0240200029031028'), 'HAS_SCALEFORM_MOVIE_LOADED native entry')
    native[at:at] = MOVIE_LOADED_NATIVE_CHECK

    stats = body(STATS_UPDATE); at = locals_end(stats)
    check(stats[at:at + 9] == bytes.fromhex('230042407c22052400'), 'CStatsMgr::Update entry')
    stats[at:at] = cash_hotkey()

    # GrowHeap entry: if (heapCount <= 31) grow; the constant becomes heaps - 1 (same 1-byte LEB).
    grow = body(GROW_HEAP); at = locals_end(grow)
    check(grow[at:at + 13] == bytes.fromhex('024020002802fc032210411f4d'), 'GrowHeap entry')
    grow[at + 11] = heaps - 1

    # With the cap, allocations can fail: "fits" 1 -> 0, and loadCompletely 256 -> 0 (same two-byte LEB).
    for fn, test, at_byte, what in ((LOAD_REQUESTED, NO_ROOM_TEST, 5, 'LoadRequestedObjects no-room test'),
                                    (ADD_IMAGE, WHOLE_LOAD_FLAG, 4, 'AddImageToList loadCompletely flag')):
        code = body(fn); at = code.find(test)
        check(at >= 0 and code.count(test) == 1, what)
        code[at + at_byte] = 0

    # First thing in each wait loop: if (spoolThreadId == -1) break out. In Flush, br_if 1 leaves the loop and its if.
    # In HandleBufferSwapFailure, br_if 3 leaves the wait, the swap retry loop and the block around them: the existing
    # early return, after which the caller drops the line.
    for fn, loop, depth, what in ((LOG_FLUSH, FLUSH_WAIT_LOOP, 1, 'Flush wait loop'),
                                  (LOG_SWAP_FAILURE, SWAP_WAIT_LOOP, 3, 'HandleBufferSwapFailure wait loop')):
        code = body(fn)
        at = code.find(loop)
        check(at >= 0 and code.count(loop) == 1, f'netLogImpl::{what}')
        at += len(loop)
        code[at:at] = b'\x42' + sleb(SPOOL_THREAD_ID) + asm('290300 427f 51 0d') + bytes([depth])  # i64.load; i64.const -1; i64.eq; br_if

    # fiDeviceMemory::Create: name + 8 -> name + 10, past the "0x" of %p (a null %p is 16 zeros, still read as 0).
    code = body(MEMORY_CREATE); at = code.find(MEMORY_NAME_TEST)
    check(at >= 0 and code.count(MEMORY_NAME_TEST) == 1, 'fiDeviceMemory::Create name offset')
    code[at + 18] = 10

    for fn in MOVIE_WAITS:
        check(starts.get(fn[1], -1) + first_index == fn[0], f'func[{fn[0]}] offset')
        movie = body(fn); at = movie.find(MOVIE_WAIT_TEST)
        check(at >= 0 and movie.count(MOVIE_WAIT_TEST) == 1, f'func[{fn[0]}] WaitTillLoaded test')
        movie[at + 5] = 0x72  # i32.or

    payload = enc_leb(count) + b''.join(enc_leb(len(b)) + b for b in bodies)
    section = bytes([10]) + enc_leb(len(payload)) + payload
    data = data[:section_start] + section + data[section_end:]
    check(data.count(DEBUG_DRAW_CHECK) == 21, 'script debug draw checks')
    return data.replace(DEBUG_DRAW_CHECK, DEBUG_DRAW_OFF + DEBUG_DRAW_CHECK[len(DEBUG_DRAW_OFF):])


def add_photo_pipeline():
    for name in ('pipelines.json', 'pipelines_low.json'):
        path = DIR / 'shaders' / name
        if not path.exists():
            print(f'{name}: not in your mirror, photo pipeline not added (the game runs without it)')
            continue
        recipes = json.loads(path.read_text())
        if PHOTO_PIPELINE not in recipes:
            recipes.append(PHOTO_PIPELINE)
            path.write_text(json.dumps(recipes, separators=(',', ':')))
        print(f'{name}: photo pipeline present ({len(recipes)} recipes)')


def scaled_look(meta):
    fixed = meta.replace(b'\r\n', b'\n').replace(b'</AxisDefinitions>', SCALED_LOOK_AXES + b'</AxisDefinitions>', 1)
    return fixed + b' ' * (len(meta) - len(fixed))


def add_scaled_look():
    orig = SETTINGS.with_name('settings.meta.orig')
    if not orig.exists():
        if hashlib.sha256(SETTINGS.read_bytes()).hexdigest() != SETTINGS_SHA256:
            print('settings.meta: not the expected file, camera fix not written')
            return
        shutil.copy2(SETTINGS, orig)
    source = orig.read_bytes()
    if hashlib.sha256(source).hexdigest() != SETTINGS_SHA256:
        print('settings.meta.orig: not the expected file, camera fix not written')
        return
    SETTINGS.write_bytes(scaled_look(source))
    print('settings.meta: scaled look axes present (script cameras)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--heaps', type=int, help='streaming cache size in 256 MB heaps, 1-32 (default: from RAM, 6-7)')
    ram = ram_gb()
    heaps = parser.parse_args().heaps
    heaps = heap_limit(ram) if heaps is None else heaps
    if not 1 <= heaps <= 32:
        parser.error('--heaps must be 1-32')
    orig = DIR / 'game.wasm.orig'
    if not orig.exists():
        check(hashlib.sha256((DIR / 'game.wasm').read_bytes()).hexdigest() == ORIG_SHA256, 'game.wasm sha256')
        shutil.copy2(DIR / 'game.wasm', orig)
    source = orig.read_bytes()
    check(hashlib.sha256(source).hexdigest() == ORIG_SHA256, 'game.wasm.orig sha256')
    patched = patch(source, heaps)
    (DIR / 'game.wasm').write_bytes(patched)
    print(f'game.wasm patched: {len(source)} -> {len(patched)} bytes, sha256 {hashlib.sha256(patched).hexdigest()}')
    print(f'streaming cache: {heaps * 256} MB ({heaps} x 256 MB, RAM {f"{ram:.0f} GB" if ram else "unknown"})')
    add_photo_pipeline()
    add_scaled_look()
