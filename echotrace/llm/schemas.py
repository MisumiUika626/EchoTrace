from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SECTIONS = ["identity", "language_dna", "emotion", "values", "interaction", "narrative", "advice", "unknowns"]
RADAR_KEYS = ["affinity", "intimacy", "trust", "dependence", "initiative", "stability", "openness", "progression"]


class Record(BaseModel):
    model_config = ConfigDict(extra="allow")


class Claim(Record):
    value: str = Field(min_length=1)
    evidence: list[int] = Field(min_length=1)
    inferred: bool
    confidence: float = Field(ge=0, le=1)
    note: str = ""

    @model_validator(mode="after")
    def inference_note(self):
        if self.inferred and not self.note.strip():
            raise ValueError("推断必须注明推断依据")
        return self


class Observation(Claim):
    dimension: Literal["personality", "expression_dna", "emotion", "interests", "relationship", "events"]
    quote: str
    date: str


class Chunk(Record):
    observations: list[Observation]
    open_questions: list[str] = []


class Section(Record):
    key: str
    title: str
    items: list[Claim]


class Persona(Record):
    sections: list[Section] = Field(min_length=8, max_length=8)
    tensions: list[Claim] = []
    unknowns: list[str]

    @model_validator(mode="after")
    def sections_valid(self):
        if {s.key for s in self.sections} != set(SECTIONS):
            raise ValueError("画像八个板块必须与 schema 约定一致")
        return self


class Dimension(Record):
    key: str
    model_score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    trend: Literal["rising", "flat", "falling"]
    evidence: list[int] = Field(min_length=3)
    interpretation: str
    conflict_note: str = ""


class Radar(Record):
    dimensions: list[Dimension] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def dimensions_valid(self):
        if {d.key for d in self.dimensions} != set(RADAR_KEYS):
            raise ValueError("雷达维度必须为约定的八项")
        if any(len(set(d.evidence)) < 3 for d in self.dimensions):
            raise ValueError("每维必须至少三个不同的消息证据")
        return self


class Quote(Record):
    evidence: list[int] = Field(min_length=1, max_length=7)
    text: str
    topic: str
    reuse: float = Field(ge=0, le=10)
    emotion: float = Field(ge=0, le=10)
    relation: float = Field(ge=0, le=10)
    info: float = Field(ge=0, le=10)
    reason: str
    sensitive: bool


class Quotes(Record):
    quotes: list[Quote]


class MentalModel(Claim):
    name: str
    application: str
    limitation: str


class Skill(Record):
    models: list[MentalModel] = Field(min_length=3, max_length=7)
    heuristics: list[Claim] = Field(min_length=5, max_length=10)


class SelfStyle(Record):
    sentence_length: Claim
    punctuation: Claim
    emoji: Claim
    catchphrases: list[Claim]
    rhythm: Claim


class Risk(Record):
    level: Literal["low", "medium", "high"]
    detail: str


class Option(Record):
    strategy: Literal["safe", "warm", "honest"]
    text: str = Field(min_length=1)
    why: str
    evidence: list[int] = Field(min_length=1)
    predicted_reaction: str
    risk: Risk
    follow_up: str


class Advice(Record):
    situation_read: str
    options: list[Option] = Field(min_length=3, max_length=3)
    avoid: list[str]
    confidence_note: str

    @model_validator(mode="after")
    def options_valid(self):
        if {x.strategy for x in self.options} != {"safe", "warm", "honest"}:
            raise ValueError("三种策略必须各出现一次")
        if len({x.text.strip() for x in self.options}) != 3:
            raise ValueError("三条回复不得重复")
        return self


class Chat(Record):
    text: str
    evidence: list[int]
    inferred: bool


SCHEMAS = {"distill_chunk": Chunk, "distill_persona": Persona, "distill_radar": Radar,
           "extract_quotes": Quotes, "distill_skill": Skill, "self_style": SelfStyle,
           "advise_top3": Advice, "chat_roleplay": Chat}
