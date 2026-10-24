from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SECTIONS = ["identity", "language_dna", "emotion", "values", "interaction", "narrative", "advice", "unknowns"]
RADAR_KEYS = ["affinity", "intimacy", "trust", "dependence", "initiative", "stability", "openness", "progression"]


class Record(BaseModel):
    model_config = ConfigDict(extra="allow", str_max_length=2000)


class Claim(Record):
    value: str = Field(min_length=1, max_length=600)
    evidence: list[int] = Field(min_length=1, max_length=12)
    inferred: bool
    confidence: float = Field(ge=0, le=1)
    note: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def inference_note(self):
        if self.inferred and not self.note.strip():
            raise ValueError("推断必须注明推断依据")
        return self


class Observation(Claim):
    dimension: Literal["personality", "expression_dna", "emotion", "interests", "relationship", "events"]
    quote: str = Field(max_length=800)
    date: str = Field(max_length=32)


class Chunk(Record):
    observations: list[Observation] = Field(max_length=24)
    open_questions: list[str] = Field(default_factory=list, max_length=8)


class Section(Record):
    key: str
    title: str
    items: list[Claim] = Field(max_length=6)


class Persona(Record):
    sections: list[Section] = Field(min_length=8, max_length=8)
    tensions: list[Claim] = Field(default_factory=list, max_length=6)
    unknowns: list[str] = Field(max_length=12)

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
    evidence: list[int] = Field(min_length=3, max_length=12)
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
    text: str = Field(min_length=1, max_length=800)
    topic: str
    reuse: float = Field(ge=0, le=10)
    emotion: float = Field(ge=0, le=10)
    relation: float = Field(ge=0, le=10)
    info: float = Field(ge=0, le=10)
    reason: str
    sensitive: bool


class Quotes(Record):
    quotes: list[Quote] = Field(max_length=24)


class MentalModel(Claim):
    name: str = Field(max_length=100)
    application: str = Field(max_length=400)
    limitation: str = Field(max_length=300)


class Skill(Record):
    models: list[MentalModel] = Field(min_length=3, max_length=7)
    heuristics: list[Claim] = Field(min_length=5, max_length=10)


class SelfStyle(Record):
    sentence_length: Claim
    punctuation: Claim
    emoji: Claim
    catchphrases: list[Claim] = Field(max_length=8)
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


class ResearchClaim(Claim):
    source_kind: Literal["direct", "reported", "inference"]

    @model_validator(mode="after")
    def source_valid(self):
        if self.source_kind == "inference" and not self.inferred:
            raise ValueError("推测来源必须标明 inferred")
        if self.source_kind == "reported" and not self.note.strip():
            raise ValueError("转述须注明发言者与可信度限制")
        return self


class NuwaResearch(Record):
    findings: list[ResearchClaim] = Field(max_length=16)
    contradictions: list[ResearchClaim] = Field(max_length=6)
    gaps: list[str] = Field(max_length=12)


class Scenario(Record):
    domain: str = Field(min_length=1)
    evidence: list[int] = Field(min_length=1, max_length=12)


class Candidate(MentalModel):
    scenarios: list[Scenario] = Field(max_length=6)
    generative: bool
    generative_reason: str = Field(max_length=300)
    distinctive: bool
    distinctive_reason: str = Field(max_length=300)


class NuwaSynthesis(Record):
    candidates: list[Candidate] = Field(max_length=10)
    heuristics: list[Claim] = Field(max_length=10)
    expression: list[Claim] = Field(max_length=8)
    values: list[Claim] = Field(max_length=8)
    tensions: list[Claim] = Field(max_length=6)
    lineage: list[Claim] = Field(max_length=6)
    unknowns: list[str] = Field(max_length=12)


class TestQuestion(Record):
    id: str
    question: str
    expected: Claim


class NuwaTestPlan(Record):
    known: list[TestQuestion] = Field(max_length=3)
    edge_question: str
    voice_question: str

    @model_validator(mode="after")
    def unique_ids(self):
        if len({q.id for q in self.known}) != len(self.known) or any(q.id in ("edge", "voice") for q in self.known):
            raise ValueError("已知问题 ID 必须唯一且不能为 edge 或 voice")
        return self


class TestAnswer(Record):
    id: str
    text: str
    evidence: list[int]
    inferred: bool


class NuwaResponses(Record):
    answers: list[TestAnswer] = Field(min_length=2, max_length=5)


class TestCheck(Record):
    id: str
    passed: bool
    reason: str
    evidence: list[int]


class NuwaEvaluation(Record):
    checks: list[TestCheck] = Field(min_length=2, max_length=5)
    improvements: list[str] = Field(max_length=8)


class ReviewScore(Record):
    dimension: str
    score: float = Field(ge=0, le=10)
    reason: str


class Suggestion(Record):
    section: Literal["roles", "protocol", "boundaries"]
    text: str = Field(min_length=1, max_length=1000)
    reason: str


class NuwaRefinement(Record):
    perspective: Literal["optimizer", "creator"]
    scores: list[ReviewScore] = Field(max_length=10)
    dry_runs: list[str] = Field(max_length=3)
    suggestions: list[Suggestion] = Field(max_length=3)


class ProposalDecision(Record):
    index: int = Field(ge=0, le=5)
    accepted: bool
    reason: str


class NuwaIntegration(Record):
    decisions: list[ProposalDecision] = Field(max_length=6)


SCHEMAS.update(nuwa_research=NuwaResearch, nuwa_synthesize=NuwaSynthesis,
               nuwa_test_plan=NuwaTestPlan, nuwa_respond=NuwaResponses,
               nuwa_evaluate=NuwaEvaluation, nuwa_refine=NuwaRefinement, nuwa_integrate=NuwaIntegration)
