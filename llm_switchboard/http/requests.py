"""Request documents; domain validators retain their existing error precedence.

The legacy actions ignore unknown fields and sometimes coerce values (notably
`keep`). SkipValidation describes the accepted form without introducing new
HTTP errors before the domain has checked the session's state and ownership.
"""

from pydantic import BaseModel, ConfigDict, SkipValidation, create_model, model_validator

from .. import settings
from .records import http_type


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    def payload(self) -> dict:
        return self.model_dump(exclude_unset=True, warnings=False)


class NameBody(ActionBody):
    name: SkipValidation[str] = ""


class RenameBody(ActionBody):
    title: SkipValidation[str] = None


class MoveBody(ActionBody):
    cwd: SkipValidation[str] = None


class RemoteForkBody(RenameBody, MoveBody):
    pass


class ForkBody(RemoteForkBody):
    keep: SkipValidation[bool] = False


class CreateBody(NameBody, MoveBody):
    tool: SkipValidation[str] = "claude"
    prompt: SkipValidation[str] = None


class SettingsValidation(ActionBody):
    @model_validator(mode="before")
    @classmethod
    def validate_settings(cls, value):
        return settings.validate(value)


# Partial fields come from the same stdlib records as the settings CLI. Nested
# partial updates are intentional, so the folder fields also have defaults.
PartialFolders = create_model("PartialFolders", __base__=ActionBody,
                              **{key: (http_type(value), None)
                                 for key, value in settings.FolderSettings.__annotations__.items()})
SettingsBody = create_model("SettingsBody", __base__=SettingsValidation,
                            **{key: (PartialFolders if key == "folders" else http_type(value), None)
                               for key, value in settings.Settings.__annotations__.items()})
