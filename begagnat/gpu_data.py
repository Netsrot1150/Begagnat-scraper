"""Knowledge base of GPUs that matter for running LLMs locally.

Each entry carries the information you actually need when judging a used
listing: how much VRAM you get, how fast the card is at inference, and what
the practical gotchas are (cooling, drivers, compute capability).

``vram_gb`` is the variant you get unless the listing says otherwise;
``vram_variants`` lists every capacity the model shipped in, so a title like
"Tesla M40 24GB" resolves to the 24 GB variant instead of the default.
"""

from __future__ import annotations

# ai_tier:
#   "great"   - modern, fast, well supported by every inference stack
#   "good"    - solid value, minor caveats
#   "ok"      - works, but expect slow prompt processing or missing kernels
#   "poor"    - only worth it if nearly free; expect real pain
GPUS: list[dict] = [
    # ---------------------------------------------------------------- NVIDIA datacenter
    {
        "name": "Tesla K80",
        "aliases": ["tesla k80", "k80"],
        "vram_gb": 24, "vram_variants": [24], "dies": 2,
        "vendor": "NVIDIA", "arch": "Kepler", "year": 2014, "compute": 3.7,
        "bandwidth_gbs": 480, "tdp_w": 300, "ai_tier": "poor",
        "notes": "Two 12 GB GPUs on one board - no model sees 24 GB. Dropped by CUDA 12 "
                 "and by current llama.cpp/PyTorch builds. Passive cooling. Avoid.",
    },
    {
        "name": "Tesla M40",
        "aliases": ["tesla m40", "m40"],
        "vram_gb": 24, "vram_variants": [12, 24],
        "vendor": "NVIDIA", "arch": "Maxwell", "year": 2015, "compute": 5.2,
        "bandwidth_gbs": 288, "tdp_w": 250, "ai_tier": "poor",
        "notes": "Cheap 24 GB but Maxwell: no usable fp16, very slow prompt processing. "
                 "Passive cooling, needs a blower shroud. Only for bulk VRAM on a budget.",
    },
    {
        "name": "Tesla P4",
        "aliases": ["tesla p4"],
        "vram_gb": 8, "vram_variants": [8],
        "vendor": "NVIDIA", "arch": "Pascal", "year": 2016, "compute": 6.1,
        "bandwidth_gbs": 192, "tdp_w": 75, "ai_tier": "ok",
        "notes": "Single-slot, 75 W, no extra power connector - nice for small servers. "
                 "Only 8 GB, so it is a companion card, not a main card.",
    },
    {
        "name": "Tesla P40",
        "aliases": ["tesla p40", "p40", "p 40"],
        "vram_gb": 24, "vram_variants": [24],
        "vendor": "NVIDIA", "arch": "Pascal", "year": 2016, "compute": 6.1,
        "bandwidth_gbs": 346, "tdp_w": 250, "ai_tier": "good",
        "notes": "The classic cheap 24 GB card. Good int8/GGUF throughput in llama.cpp, "
                 "but fp16 is crippled (1:64) so vLLM/exllama are off the table. "
                 "Passive: needs a blower shroud and a server-grade PSU cable (EPS, not PCIe).",
    },
    {
        "name": "Tesla P100",
        "aliases": ["tesla p100", "p100", "p 100"],
        "vram_gb": 16, "vram_variants": [12, 16],
        "vendor": "NVIDIA", "arch": "Pascal", "year": 2016, "compute": 6.0,
        "bandwidth_gbs": 732, "tdp_w": 250, "ai_tier": "good",
        "notes": "HBM2 gives it 732 GB/s - roughly 2x a P40 in token generation. "
                 "Real fp16 (unlike P40), but compute 6.0 means no bf16, no FlashAttention-2. "
                 "llama.cpp and older vLLM builds work. Passive, needs a blower shroud.",
    },
    {
        "name": "Tesla V100",
        "aliases": ["tesla v100", "v100"],
        "vram_gb": 16, "vram_variants": [16, 32],
        "vendor": "NVIDIA", "arch": "Volta", "year": 2017, "compute": 7.0,
        "bandwidth_gbs": 900, "tdp_w": 250, "ai_tier": "great",
        "notes": "First-gen tensor cores, 900 GB/s. Excellent inference card. Watch the form "
                 "factor: SXM2 versions need a server board, only PCIe cards drop into a desktop.",
    },
    {
        "name": "Tesla T4",
        "aliases": ["tesla t4", "nvidia t4"],
        "vram_gb": 16, "vram_variants": [16],
        "vendor": "NVIDIA", "arch": "Turing", "year": 2018, "compute": 7.5,
        "bandwidth_gbs": 320, "tdp_w": 70, "ai_tier": "good",
        "notes": "70 W single-slot with int8/int4 tensor cores. Low bandwidth caps token "
                 "speed, but it is the easiest datacenter card to actually fit and power.",
    },
    {
        "name": "NVIDIA A10",
        "aliases": ["nvidia a10", "a10 gpu"],
        "vram_gb": 24, "vram_variants": [24],
        "vendor": "NVIDIA", "arch": "Ampere", "year": 2021, "compute": 8.6,
        "bandwidth_gbs": 600, "tdp_w": 150, "ai_tier": "great",
        "notes": "24 GB, bf16, single-slot 150 W. Effectively a server-cooled 3090-lite. "
                 "Everything modern runs on it.",
    },
    {
        "name": "NVIDIA L4",
        "aliases": ["nvidia l4", "l4 gpu"],
        "vram_gb": 24, "vram_variants": [24],
        "vendor": "NVIDIA", "arch": "Ada", "year": 2023, "compute": 8.9,
        "bandwidth_gbs": 300, "tdp_w": 72, "ai_tier": "great",
        "notes": "24 GB at 72 W with fp8 support. Bandwidth-limited but extremely easy to host.",
    },
    {
        "name": "NVIDIA A100",
        "aliases": ["a100", "nvidia a100"],
        "vram_gb": 40, "vram_variants": [40, 80],
        "vendor": "NVIDIA", "arch": "Ampere", "year": 2020, "compute": 8.0,
        "bandwidth_gbs": 1555, "tdp_w": 300, "ai_tier": "great",
        "notes": "The real thing. Check SXM4 vs PCIe - SXM4 boards are useless without an "
                 "HGX baseboard and are the usual reason a listing looks suspiciously cheap.",
    },
    # ---------------------------------------------------------------- NVIDIA workstation
    {
        "name": "RTX A4000",
        "aliases": ["rtx a4000", "quadro a4000"],
        "vram_gb": 16, "vram_variants": [16],
        "vendor": "NVIDIA", "arch": "Ampere", "year": 2021, "compute": 8.6,
        "bandwidth_gbs": 448, "tdp_w": 140, "ai_tier": "great",
        "notes": "Single-slot 16 GB, blower cooled, 140 W. The tidiest way to stack cards "
                 "in a normal case.",
    },
    {
        "name": "RTX A4500",
        "aliases": ["rtx a4500"],
        "vram_gb": 20, "vram_variants": [20],
        "vendor": "NVIDIA", "arch": "Ampere", "year": 2021, "compute": 8.6,
        "bandwidth_gbs": 640, "tdp_w": 200, "ai_tier": "great",
        "notes": "20 GB blower card, dual-slot.",
    },
    {
        "name": "RTX A5000",
        "aliases": ["rtx a5000"],
        "vram_gb": 24, "vram_variants": [24],
        "vendor": "NVIDIA", "arch": "Ampere", "year": 2021, "compute": 8.6,
        "bandwidth_gbs": 768, "tdp_w": 230, "ai_tier": "great",
        "notes": "24 GB blower card with NVLink. Quieter and slimmer than a 3090 for the "
                 "same VRAM, a bit slower.",
    },
    {
        "name": "RTX A6000",
        "aliases": ["rtx a6000", "a6000"],
        "vram_gb": 48, "vram_variants": [48],
        "vendor": "NVIDIA", "arch": "Ampere", "year": 2020, "compute": 8.6,
        "bandwidth_gbs": 768, "tdp_w": 300, "ai_tier": "great",
        "notes": "48 GB in one dual-slot blower card - a 70B model at 4-bit fits on one card.",
    },
    {
        "name": "RTX 4000 Ada",
        "aliases": ["rtx 4000 ada", "rtx 4000 sff ada"],
        "vram_gb": 20, "vram_variants": [20],
        "vendor": "NVIDIA", "arch": "Ada", "year": 2023, "compute": 8.9,
        "bandwidth_gbs": 360, "tdp_w": 130, "ai_tier": "great",
        "notes": "20 GB, 130 W, single slot. Expensive per GB but effortless to install.",
    },
    {
        "name": "RTX 5000 Ada",
        "aliases": ["rtx 5000 ada"],
        "vram_gb": 32, "vram_variants": [32],
        "vendor": "NVIDIA", "arch": "Ada", "year": 2023, "compute": 8.9,
        "bandwidth_gbs": 576, "tdp_w": 250, "ai_tier": "great",
        "notes": "32 GB blower card with fp8.",
    },
    {
        "name": "RTX 6000 Ada",
        "aliases": ["rtx 6000 ada"],
        "vram_gb": 48, "vram_variants": [48],
        "vendor": "NVIDIA", "arch": "Ada", "year": 2022, "compute": 8.9,
        "bandwidth_gbs": 960, "tdp_w": 300, "ai_tier": "great",
        "notes": "48 GB, fp8, 960 GB/s. The best single-card option short of datacenter parts.",
    },
    {
        "name": "Titan RTX",
        "aliases": ["titan rtx"],
        "vram_gb": 24, "vram_variants": [24],
        "vendor": "NVIDIA", "arch": "Turing", "year": 2018, "compute": 7.5,
        "bandwidth_gbs": 672, "tdp_w": 280, "ai_tier": "good",
        "notes": "24 GB Turing with a normal fan. No bf16, but exllama and llama.cpp are happy.",
    },
    # ---------------------------------------------------------------- GeForce
    {
        "name": "GTX 1080 Ti",
        "aliases": ["1080 ti", "gtx 1080ti", "gtx 1080 ti"],
        "vram_gb": 11, "vram_variants": [11],
        "vendor": "NVIDIA", "arch": "Pascal", "year": 2017, "compute": 6.1,
        "bandwidth_gbs": 484, "tdp_w": 250, "ai_tier": "ok",
        "notes": "11 GB and crippled fp16 like the P40, but it has a fan and drops into any PC.",
    },
    {
        "name": "RTX 2080 Ti",
        "aliases": ["2080 ti", "rtx 2080ti", "rtx 2080 ti"],
        "vram_gb": 11, "vram_variants": [11, 22],
        "vendor": "NVIDIA", "arch": "Turing", "year": 2018, "compute": 7.5,
        "bandwidth_gbs": 616, "tdp_w": 250, "ai_tier": "good",
        "notes": "Watch for 22 GB memory-modded cards from China - great value per GB when "
                 "they work, no warranty and occasional VBIOS trouble when they don't.",
    },
    {
        "name": "RTX 3060",
        "aliases": ["rtx 3060", "3060 12gb"],
        "vram_gb": 12, "vram_variants": [8, 12],
        "vendor": "NVIDIA", "arch": "Ampere", "year": 2021, "compute": 8.6,
        "bandwidth_gbs": 360, "tdp_w": 170, "ai_tier": "good",
        "notes": "The cheapest modern 12 GB card. Make sure it is the 12 GB version, not 8 GB.",
    },
    {
        "name": "RTX 3090",
        "aliases": ["rtx 3090", "3090 ti", "rtx 3090 ti"],
        "vram_gb": 24, "vram_variants": [24],
        "vendor": "NVIDIA", "arch": "Ampere", "year": 2020, "compute": 8.6,
        "bandwidth_gbs": 936, "tdp_w": 350, "ai_tier": "great",
        "notes": "Still the value king for local AI: 24 GB, 936 GB/s, bf16, NVLink, and every "
                 "stack supports it. Check the memory temperature - GDDR6X on the back runs hot.",
    },
    {
        "name": "RTX 4060 Ti 16GB",
        "aliases": ["4060 ti 16gb", "rtx 4060 ti 16"],
        "vram_gb": 16, "vram_variants": [16],
        "vendor": "NVIDIA", "arch": "Ada", "year": 2023, "compute": 8.9,
        "bandwidth_gbs": 288, "tdp_w": 165, "ai_tier": "good",
        "notes": "16 GB at 165 W, but only 288 GB/s - generation speed is the bottleneck.",
    },
    {
        "name": "RTX 4090",
        "aliases": ["rtx 4090", "4090"],
        "vram_gb": 24, "vram_variants": [24],
        "vendor": "NVIDIA", "arch": "Ada", "year": 2022, "compute": 8.9,
        "bandwidth_gbs": 1008, "tdp_w": 450, "ai_tier": "great",
        "notes": "Fastest consumer inference card with 24 GB. Rarely a bargain second-hand.",
    },
    {
        "name": "RTX 5090",
        "aliases": ["rtx 5090", "5090"],
        "vram_gb": 32, "vram_variants": [32],
        "vendor": "NVIDIA", "arch": "Blackwell", "year": 2025, "compute": 12.0,
        "bandwidth_gbs": 1792, "tdp_w": 575, "ai_tier": "great",
        "notes": "32 GB and 1.8 TB/s. Needs recent CUDA 12.8+ builds of your inference stack.",
    },
    # ---------------------------------------------------------------- AMD
    {
        "name": "Radeon Instinct MI25",
        "aliases": ["mi25", "instinct mi25"],
        "vram_gb": 16, "vram_variants": [16],
        "vendor": "AMD", "arch": "Vega", "year": 2017,
        "bandwidth_gbs": 484, "tdp_w": 300, "ai_tier": "poor",
        "notes": "16 GB HBM2 for pocket change, but ROCm dropped Vega support - you are on "
                 "old ROCm or Vulkan llama.cpp. Passive, and needs a display-output BIOS flash.",
    },
    {
        "name": "Radeon Instinct MI50",
        "aliases": ["mi50", "instinct mi50"],
        "vram_gb": 16, "vram_variants": [16, 32],
        "vendor": "AMD", "arch": "Vega 20", "year": 2018,
        "bandwidth_gbs": 1024, "tdp_w": 300, "ai_tier": "ok",
        "notes": "1 TB/s HBM2 and 32 GB variants exist - superb bandwidth per krona. ROCm "
                 "support ended at 5.7, so pin your stack or use Vulkan. Passive cooling.",
    },
    {
        "name": "Radeon VII",
        "aliases": ["radeon vii", "radeon 7"],
        "vram_gb": 16, "vram_variants": [16],
        "vendor": "AMD", "arch": "Vega 20", "year": 2019,
        "bandwidth_gbs": 1024, "tdp_w": 300, "ai_tier": "ok",
        "notes": "MI50 with a fan and a display output. Same ROCm caveats.",
    },
    {
        "name": "Radeon RX 7900 XTX",
        "aliases": ["7900 xtx", "rx 7900xtx"],
        "vram_gb": 24, "vram_variants": [24],
        "vendor": "AMD", "arch": "RDNA3", "year": 2022,
        "bandwidth_gbs": 960, "tdp_w": 355, "ai_tier": "good",
        "notes": "24 GB with current ROCm support on Linux. llama.cpp and vLLM work; expect "
                 "more setup friction than NVIDIA.",
    },
    {
        "name": "Radeon Pro W6800",
        "aliases": ["w6800", "radeon pro w6800"],
        "vram_gb": 32, "vram_variants": [32],
        "vendor": "AMD", "arch": "RDNA2", "year": 2021,
        "bandwidth_gbs": 512, "tdp_w": 250, "ai_tier": "ok",
        "notes": "32 GB blower card. ROCm works but RDNA2 compute support is thin.",
    },
    # ---------------------------------------------------------------- Intel
    {
        "name": "Intel Arc A770",
        "aliases": ["arc a770", "a770"],
        "vram_gb": 16, "vram_variants": [8, 16],
        "vendor": "Intel", "arch": "Alchemist", "year": 2022,
        "bandwidth_gbs": 560, "tdp_w": 225, "ai_tier": "ok",
        "notes": "16 GB cheap, and llama.cpp SYCL/Vulkan works. Ecosystem is still thin - "
                 "fine for GGUF inference, awkward for anything else.",
    },
]

# Words that disqualify a listing.
#
# The split matters: a description reading "passivt kylt, behöver fläktshroud"
# is a *good* P100 listing, so accessory words are only trusted in the title.
# Condition and form-factor words are trusted anywhere, because "säljes för
# delar" and "SXM2" are disqualifying wherever the seller mentions them.
ACCESSORY_HINTS = [
    "kylare", "kylfläns", "vattenblock", "backplate", "stödben", "riser",
    "cooler only", "heatsink only", "shroud", "waterblock", "bracket", "adapter",
    "tröja", "t-shirt", "mugg", "poster", "figur", "sticker", "dekal",
    # Cases sold by listing every card they fit ("FormD T1 RTX 5080 5090 FE
    # Travel Kit" - seen live on Tradera 2026-09-04).
    "chassi", "travel kit",
    # eBay is English and German, and its accessory sellers name every card
    # their part fits: "Cooling Fan for Nvidia Tesla V100 P100 P40 M40 K80".
    # Half of the first live eBay run was fans, cables and brackets. Each
    # needle has to be narrow enough to spare a real card that mentions its
    # cooler ("Tesla P40 24GB CUDA GPU - aktiver Lüfter" is a card, with a fan).
    "cooling fan", "cooling turbine", "turbine fan", "kühlventilator",
    "fan for", "cooler for", "kühler für", "lüfter für",
    "power cable", "netzkabel", "cable for", "kabel für",
    "8pin to", "8 pin to", "halterung", "rfid", "nfc tag",
]

CONDITION_HINTS = [
    "trasig", "defekt", "reservdel", "för delar", "ej fungerande", "skrot",
    "broken", "faulty", "for parts", "not working", "spares or repair",
]

FORM_FACTOR_HINTS = [
    "sxm2", "sxm3", "sxm4", "sxm5",   # server module, not a PCIe card
    "mezzanine", "om-shell",
]
