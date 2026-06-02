"""Reznar's Arcane Oddities — domain ontology.

--------------------------------------------------------------------------
DESIGN CHOICES (why the model looks like this)
--------------------------------------------------------------------------
The catalog has exactly ONE entity, `MagicItem`. Everything Reznar cares
about is an attribute of an item, so a single entity (one DB row + UUID
each) is the honest model — no extra tables that would only add joins.

The deliberate move here is to record TWO notions of "category":

  * `printed_type` — the word(s) literally printed under the item's name
    on the page ("Wondrous item", "Armor", "Potion"). Kept VERBATIM because
    the source is inconsistent and we don't want to lose what it actually
    said. This is the catalog's own (unreliable) label.

  * `actual_form` — what the item REALLY is, judged from the illustration
    (worn on the head, a ring, a sword, a drinkable potion, a charm, ...).
    This is the ground truth we trust. It also answers Reznar's
    one-object-per-slot concern: for worn items the form IS the body slot,
    so two HEAD items can't be used together.

Splitting them lets us cross-check the catalog against reality (e.g. an
item printed as "Wondrous item" that is actually worn on the head) and is
the basis for the Part-2 rarity analysis.

Rarity is a closed controlled vocabulary (enum) because the set is fixed
(common .. artifact) and Part 2 is entirely about reasoning over it.

Attunement is two fields: a boolean plus an optional free-text condition,
because some items say only "(requires attunement)" while others restrict
it ("requires attunement by an elf").

Graceful degradation: the PDF is imperfect, so the rarity/form normalizers
fall back to an UNKNOWN member instead of raising. We would rather keep a
row flagged "unknown" than drop the item. `source_page` is light
provenance so a human can audit any questionable extraction.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Optional
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


# ---- Hint marker (carried as Annotated metadata, mirrors stormland) ----------

class Hint:
    """Free-text description for LLM agents, carried as Annotated metadata."""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


# ---- Controlled vocabularies (closed enums) ---------------------------------

class Rarity(str, Enum):
    COMMON = "common"
    UNCOMMON = "uncommon"
    RARE = "rare"
    VERY_RARE = "very_rare"
    LEGENDARY = "legendary"
    ARTIFACT = "artifact"
    UNKNOWN = "unknown"


class ActualForm(str, Enum):
    """What the item REALLY is, judged from its illustration.

    For worn items the value doubles as the body slot (HEAD, NECK, ...),
    which is what enforces Reznar's "one object per slot" rule. Non-worn
    forms (POTION, CHARM, WEAPON, ...) describe how the item is used.
    """

    # --- worn (the value is also the body slot) ---
    HEAD = "head"             # hat, helmet, mask, headband, circlet, crown
    EYES = "eyes"             # goggles, lenses, spectacles
    NECK = "neck"             # amulet, necklace, pendant, periapt
    SHOULDERS = "shoulders"   # cloak, cape, mantle
    BODY = "body"             # armor, robe, gown, vestment
    HANDS = "hands"           # gloves, gauntlets
    RING = "ring"             # rings (finger)
    WAIST = "waist"           # belt, girdle, sash
    FEET = "feet"             # boots, shoes, slippers
    # --- wielded / held (off-hand counts as its own slot) ---
    WEAPON = "weapon"         # sword, axe, bow, etc. (wielded)
    SHIELD = "shield"         # shield / buckler (off-hand)
    HELD = "held"             # wand, rod, staff, horn, instrument, orb
    POTION = "potion"         # drinkable / consumable
    SCROLL = "scroll"         # scroll, written
    CHARM = "charm"           # stone, figurine, trinket carried for effect
    OTHER = "other"           # a real form that fits none of the above
    UNKNOWN = "unknown"       # couldn't tell from the image -> needs review


# ---- Normalizers: map messy source strings -> canonical enums ----------------

_RARITY_ALIASES = {
    "common": Rarity.COMMON,
    "uncommon": Rarity.UNCOMMON,
    "rare": Rarity.RARE,
    "very rare": Rarity.VERY_RARE,
    "veryrare": Rarity.VERY_RARE,
    "very_rare": Rarity.VERY_RARE,
    "legendary": Rarity.LEGENDARY,
    "artifact": Rarity.ARTIFACT,
}


def _to_rarity(v) -> Rarity:
    if isinstance(v, Rarity):
        return v
    return _RARITY_ALIASES.get(str(v).strip().lower(), Rarity.UNKNOWN)


# Common worn-item nouns -> form, so the model can pass either the canonical
# value ("head") or the noun it saw ("mask") and we still land on HEAD.
_FORM_ALIASES: dict[str, ActualForm] = {
    "hat": ActualForm.HEAD, "helmet": ActualForm.HEAD, "helm": ActualForm.HEAD,
    "mask": ActualForm.HEAD, "headband": ActualForm.HEAD, "circlet": ActualForm.HEAD,
    "crown": ActualForm.HEAD, "hood": ActualForm.HEAD,
    "goggles": ActualForm.EYES, "lenses": ActualForm.EYES, "spectacles": ActualForm.EYES,
    "amulet": ActualForm.NECK, "necklace": ActualForm.NECK, "pendant": ActualForm.NECK,
    "periapt": ActualForm.NECK, "medallion": ActualForm.NECK, "talisman": ActualForm.NECK,
    "cloak": ActualForm.SHOULDERS, "cape": ActualForm.SHOULDERS, "mantle": ActualForm.SHOULDERS,
    "armor": ActualForm.BODY, "armour": ActualForm.BODY, "robe": ActualForm.BODY,
    "gown": ActualForm.BODY, "vestment": ActualForm.BODY, "breastplate": ActualForm.BODY,
    "gloves": ActualForm.HANDS, "gauntlets": ActualForm.HANDS,
    "ring": ActualForm.RING,
    "belt": ActualForm.WAIST, "girdle": ActualForm.WAIST, "sash": ActualForm.WAIST,
    "boots": ActualForm.FEET, "shoes": ActualForm.FEET, "slippers": ActualForm.FEET,
    "sword": ActualForm.WEAPON, "axe": ActualForm.WEAPON, "bow": ActualForm.WEAPON,
    "dagger": ActualForm.WEAPON, "blade": ActualForm.WEAPON, "mace": ActualForm.WEAPON,
    "spear": ActualForm.WEAPON, "hammer": ActualForm.WEAPON, "whip": ActualForm.WEAPON,
    "shield": ActualForm.SHIELD, "buckler": ActualForm.SHIELD,
    "wand": ActualForm.HELD, "rod": ActualForm.HELD, "staff": ActualForm.HELD,
    "horn": ActualForm.HELD, "instrument": ActualForm.HELD, "orb": ActualForm.HELD,
    "potion": ActualForm.POTION, "oil": ActualForm.POTION, "elixir": ActualForm.POTION,
    "scroll": ActualForm.SCROLL,
    "stone": ActualForm.CHARM, "figurine": ActualForm.CHARM, "trinket": ActualForm.CHARM,
    "gem": ActualForm.CHARM, "charm": ActualForm.CHARM,
}


def _to_form(v) -> ActualForm:
    if isinstance(v, ActualForm):
        return v
    text = str(v).strip().lower()
    if not text:
        return ActualForm.UNKNOWN
    try:
        return ActualForm(text)            # already a canonical value
    except ValueError:
        pass
    for word in text.replace("-", " ").split():
        if word in _FORM_ALIASES:
            return _FORM_ALIASES[word]
    return ActualForm.UNKNOWN


def infer_form(text) -> ActualForm:
    """Public helper: best-guess form from any text (e.g. an item's name).

    Used by the pipeline as a fallback when the model can't read the form
    from the illustration — the name usually gives it away (a 'Mask of ...'
    is worn on the head, a 'Shield of ...' is a shield).
    """
    return _to_form(text)


# ---- Annotated field types (canonical form + Hint for the prompt) -----------

RarityField = Annotated[
    Rarity,
    BeforeValidator(_to_rarity),
    Hint("One of: common, uncommon, rare, very_rare, legendary, artifact. "
         "'very rare' is normalized; anything unrecognized -> 'unknown'."),
]
FormField = Annotated[
    ActualForm,
    BeforeValidator(_to_form),
    Hint("What the item REALLY is, judged from the picture. Worn items use "
         "their body slot: head, eyes, neck, shoulders, body, hands, ring, "
         "waist, feet. Otherwise: weapon, held (wand/rod/staff/horn), potion, "
         "scroll, charm, other. If unclear -> 'unknown'."),
]


# ---- The one entity ---------------------------------------------------------

class MagicItem(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    name: str = Field(min_length=1, description="the item's name as titled on the page")

    # the two notions of category
    printed_type: str = Field(
        default="",
        description="word(s) printed under the name, VERBATIM: 'Wondrous item', "
                    "'Armor', 'Potion'. The catalog's own label.")
    actual_form: FormField = ActualForm.UNKNOWN

    # rarity + attunement
    rarity: RarityField = Rarity.UNKNOWN
    requires_attunement: bool = Field(
        default=False, description="true if the page says '(requires attunement)'")
    attunement_condition: Optional[str] = Field(
        default=None,
        description="restriction text if present, e.g. 'by an elf'; null if unconditional")

    # content
    description: str = Field(default="", description="the item's rules/flavor text")

    # light provenance + db id
    source_page: Optional[int] = Field(
        default=None, description="1-based PDF page the item was extracted from")
    id: Optional[UUID] = None


# ---------------------------------------------------------------------------
# Registry — maps type-name strings to model classes so the pipeline can
# discover entities generically (mirrors stormland.REGISTRY).
# ---------------------------------------------------------------------------

REGISTRY: dict[str, type[BaseModel]] = {
    "MagicItem": MagicItem,
}
