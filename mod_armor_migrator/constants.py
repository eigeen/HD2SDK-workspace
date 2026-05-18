BaseArchiveHexID = "9ba626afa44a3aa3"

CompositeUnitID = 14191111524867688662
UnitID = 16187218042980615487
TexID = 14790446551990181426
MaterialID = 16915718763308572383
BoneID = 1792059921637536489
WwiseBankID = 6006249203084351385
WwiseDepID = 12624162998411505776
WwiseStreamID = 5785811756662211598
WwiseMetaDataID = 15351235653606224144
ParticleID = 12112766700566326628
AnimationID = 10600967118105529382
StateMachineID = 11855396184103720540
StringID = 979299457696010195
PhysicsID = 6877563742545042104

TYPE_NAMES = {
    UnitID: "Unit",
    CompositeUnitID: "CompositeUnit",
    TexID: "Texture",
    MaterialID: "Material",
    BoneID: "Bones",
    AnimationID: "Animation",
    StateMachineID: "StateMachine",
    ParticleID: "Particle",
    WwiseBankID: "WwiseBank",
    WwiseDepID: "WwiseDep",
    WwiseStreamID: "WwiseStream",
    WwiseMetaDataID: "WwiseMetadata",
    StringID: "String",
    PhysicsID: "Physics",
}

# StreamToc / package magic.
# NOTE: docs/DataStructures.md mis-prints this as 0xF0000004; the actual decimal
# value (4026531857) corresponds to 0xF0000011.
LEGACY_MAGIC = 0xF0000011
DSAR_MAGIC = 0x52415344  # "DSAR"
