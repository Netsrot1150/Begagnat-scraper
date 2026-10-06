import pytest

from begagnat.gpu import extract_quantity, extract_vram, find_negatives, identify


@pytest.mark.parametrize(
    "title,name,vram,qty",
    [
        ("Nvidia Tesla P100 16GB HBM2", "Tesla P100", 16, 1),
        ("2 st Tesla P100 16gb", "Tesla P100", 16, 2),
        ("Tesla P 40 24GB servergrafikkort", "Tesla P40", 24, 1),
        ("TESLA-P40 x2", "Tesla P40", 24, 2),
        ("RTX3090 Gaming OC", "RTX 3090", 24, 1),
        ("Radeon Instinct MI50 32GB", "Radeon Instinct MI50", 32, 1),
        ("Nvidia Tesla V100 32GB PCIe", "Tesla V100", 32, 1),
        ("Intel Arc A770 16GB", "Intel Arc A770", 16, 1),
    ],
)
def test_identify_common_titles(title, name, vram, qty):
    match = identify(title)
    assert match is not None, title
    assert match.name == name
    assert match.vram_gb == vram
    assert match.quantity == qty


def test_p100_does_not_match_p1000():
    assert identify("Nvidia Quadro P1000 4GB") is None


def test_p40_does_not_match_p4():
    match = identify("Nvidia Tesla P4 8GB")
    assert match is not None and match.name == "Tesla P4"


def test_variant_defaults_are_flagged_as_uncertain():
    match = identify("Tesla M40")
    assert match.vram_gb == 24
    assert match.confidence < 0.9


def test_explicit_variant_beats_default():
    assert identify("Tesla V100 16GB").vram_gb == 16
    assert identify("Tesla V100 32GB").vram_gb == 32


def test_vram_ignores_unrelated_numbers():
    # A 500 GB disk in a bundle listing is not the card's VRAM.
    assert extract_vram("Tesla P100 16GB + 500GB SSD", [12, 16]) == 16
    # "PCIe 3.0 x16" carries no GB unit, so there is nothing to pick.
    assert extract_vram("Tesla P100 PCIe 3.0 x16 med 500GB disk", [12, 16]) is None


def test_description_supplies_vram_when_title_omits_it():
    match = identify("Tesla V100 säljes", "Kortet är 32GB-versionen, PCIe.")
    assert match.vram_gb == 32
    assert match.confidence == pytest.approx(0.9)


def test_total_vram_multiplies_by_quantity():
    match = identify("4 st Tesla P40 24GB")
    assert match.quantity == 4
    assert match.total_vram_gb == 96


@pytest.mark.parametrize(
    "title,hint",
    [
        ("Tesla V100 SXM2 16GB", "sxm2"),
        ("Trasig RTX 3090 för delar", "trasig"),
        ("Kylare till Tesla P40", "kylare"),
        ("Nvidia Tesla P100 - broken, for parts", "broken"),
    ],
)
def test_negative_hints(title, hint):
    assert hint in find_negatives(title)


def test_quantity_defaults_to_one():
    assert extract_quantity("Tesla P100 16GB HBM2 PCIe") == 1


def test_accessory_words_in_the_description_do_not_disqualify():
    # A P100 listing that explains it needs a fan shroud is exactly what we want.
    assert find_negatives(
        "Nvidia Tesla P100 16GB", "Passivt kylt, behöver fläktshroud och kylare."
    ) == []
    # In the title, the same word means the seller is selling the shroud.
    assert "kylare" in find_negatives("Kylare till Tesla P100")


def test_condition_words_disqualify_wherever_they_appear():
    assert "för delar" in find_negatives("Tesla P40 24GB", "Säljes för delar, ger ingen bild.")
    assert "sxm2" in find_negatives("Tesla V100 16GB", "Detta är SXM2-versionen.")


@pytest.mark.parametrize(
    "title,qty",
    [
        # Real Blocket/Tradera titles: an "x" in a model name is not a count.
        ("MSI GeForce RTX 3060 Gaming X 12GB Grafikkort", 1),
        ("Sapphire Radeon RX 6800 Nitro+ Dual X 16 GB", 1),
        # Nor is a PCIe lane count.
        ("Tesla P40 24GB PCIe 3.0 x16", 1),
        ("RTX 3090 pci-e x8 riserkabel medföljer", 1),
        # Real counts still survive both guards.
        ("Tesla P100 16GB PCIe x16, säljer 2 st", 2),
        ("TESLA-P40 x2", 2),
    ],
)
def test_quantity_ignores_model_names_and_pcie_lanes(title, qty):
    """A "12GB" card read as twelve cards inflated its VRAM to 144 GB and
    slipped past a --min-vram filter. Seen live on 2026-09-04."""
    assert extract_quantity(title) == qty


def test_a_gaming_x_card_keeps_its_real_vram():
    match = identify("MSI GeForce RTX 3060 Gaming X 12GB Grafikkort")
    assert match.quantity == 1
    assert match.total_vram_gb == 12


def test_a_case_that_lists_the_cards_it_fits_is_an_accessory():
    assert "travel kit" in find_negatives("FormD T1 RTX 5080 5090 FE Travel Kit")
    assert "chassi" in find_negatives("Datorchassi med plats för RTX 4090")


@pytest.mark.parametrize(
    "title,qty",
    [
        # Cooler names count fans, not cards. All seen live on Blocket 2026-09-04.
        ("MSI GeForce RTX 3060 Ventus 2X OC 12GB grafikkort", 1),
        ("Gigabyte RTX 3090 Gaming OC 3X", 1),
        ("Zotac RTX 3090 Twin Edge 2X", 1),
        # At the head of the title it really is a bundle.
        ("2x Tesla P40 24GB", 2),
        ("Säljer 2 x Tesla P100 16GB", 2),
        ("4 st RTX 3090 ur en rigg", 4),
    ],
)
def test_a_fan_count_in_a_cooler_name_is_not_a_card_count(title, qty):
    assert extract_quantity(title) == qty


@pytest.mark.parametrize(
    "title",
    [
        "RTX 3060 Ti Grafikkort",       # 8 GB card, not the 12 GB RTX 3060
        "ASUS GeForce RTX 3060 TI OC",
    ],
)
def test_a_ti_variant_is_not_the_plain_card(title):
    assert identify(title) is None


def test_a_catalogued_ti_variant_still_matches():
    assert identify("Nvidia RTX 3090 Ti 24GB").name == "RTX 3090"
    assert identify("EVGA GeForce RTX 2080 Ti Black").name == "RTX 2080 Ti"
    assert identify("Sapphire Radeon RX 7900 XTX Nitro+").name == "Radeon RX 7900 XTX"


# Titles captured from the first live eBay Browse API run, 2026-09-05.
@pytest.mark.parametrize(
    "title",
    [
        "Cooling Turbine Fan for Nvidia Tesla V100 P100 P40 M40 K80 GPU Cooler",
        "Cooling Fan for Nvidia Tesla V100 P100 P40 M40 K80 Graphics Card U-turn Shape",
        "U-förmiger Kühlventilator für NVIDIA Tesla V100 P100 P40 M40 K80 Server",
        "NEU 1x Cooling Fan Exakt passend für NVIDIA Tesla P40 M40 V100 P100",
        "NVIDIA Tesla Graphics Card Power Cable 8 Pin PCIE for P40 P100 K80 M40 M60 V100",
        "Dual PCIe 8Pin to CPU 8Pin Power Cable for NVIDIA TESLA K80 P40 P100",
        "Dell R730 GPU Netzkabel Mini 8pin auf 8pin Tesla K80 P40 P100 kompatibel",
        "GPU Halterung Für nVidia Tesla P40 M60 P100 V100 820178-001 PK3RJ Metall",
        "Wallbox RFID Chip – beschrifteter NFC Tag, universell anlernbar (z.B. KEBA P40)",
    ],
)
def test_english_and_german_accessory_listings_are_rejected(title):
    assert find_negatives(title), f"{title!r} slipped through as a card"


@pytest.mark.parametrize(
    "title",
    [
        # A real card that ships with a fan must survive the fan keywords.
        "NVIDIA Tesla P40 24GB GDDR5 CUDA GPU – aktiver Lüfter – Ampere",
        "Nvidia Tesla P40 24GB GPU GDDR5 PCIE Graphics Accelerator with fan",
        "HP NVIDIA Tesla T4 16GB GDDR6 Active Full Height GPU Grafikkarte",
    ],
)
def test_a_card_that_mentions_its_cooling_is_still_a_card(title):
    assert find_negatives(title) == [], f"{title!r} was wrongly rejected"


@pytest.mark.parametrize(
    "title",
    [
        # Every hyphen spelling a seller uses. The first is live, ebay.com
        # 2026-09-05, and was read as sixteen cards - 256 GB on a 16 GB card.
        "NVIDIA Tesla P100 - 16GB HBM2 PCIe-x16 FH",
        "Nvidia Tesla P100 16GB Graphics Card PCI-E x16 GPU",
        "Tesla P40 24GB PCIe 3.0 x16",
        "Tesla V100 16GB PCI Express x16",
        "Tesla P4 8GB pcie-3.0-x8",
    ],
)
def test_a_pcie_lane_count_is_never_a_card_count(title):
    assert extract_quantity(title) == 1


def test_a_real_bundle_next_to_a_lane_count_still_counts():
    assert extract_quantity("2pcs Nvidia Tesla P100 16GB PCIe-x16") == 2
