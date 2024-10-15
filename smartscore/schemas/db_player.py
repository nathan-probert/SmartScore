import ctypes
from dataclasses import dataclass

from marshmallow import Schema, fields


class PlayerDbInfoC(ctypes.Structure):
    _fields_ = [
        ("date", ctypes.c_char_p),
        ("name", ctypes.c_char_p),
        ("gpg", ctypes.c_float),
        ("hgpg", ctypes.c_float),
        ("five_gpg", ctypes.c_float),
        ("tgpg", ctypes.c_float),
        ("otga", ctypes.c_float),
    ]


@dataclass(frozen=True)
class PlayerDbInfo:
    date: str

    name: str
    id: int
    team_id: int
    gpg: float
    hgpg: float
    five_gpg: float

    team_name: str
    team_abbr: str
    tgpg: float
    otga: float

    stat: float


class PlayerDbInfoSchema(Schema):
    name = fields.Str()
    id = fields.Int()
    team_id = fields.Int()

    gpg = fields.Float()
    hgpg = fields.Float()
    five_gpg = fields.Float()
    stat = fields.Float()


PLAYER_DB_INFO_SCHEMA = PlayerDbInfoSchema()
