"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""


# DEPRECATED -- use IntegrationResult directly for new code.
# Kept for backward compatibility. The fields map as follows:
# skill_created -> IntegrationResult.skill_created
# sub_skills_promoted -> IntegrationResult.sub_skills_promoted
# skill_path, references_copied -> not mapped (skill-internal)
# Bundle integration are implemented as SkillIntegrator methods in class_.py.
