from finance_ai.categories.schemas import CategoryColor, CategoryIcon, CategoryUpdateCommand
from finance_ai.core.errors import CategoryAppearanceInvalidError, RequestInvalidError


class CategoryPolicy:
    @staticmethod
    def normalize_name(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 100:
            raise RequestInvalidError({"name": ["Use a name between 1 and 100 characters."]})
        return normalized

    @staticmethod
    def validate_color(value: CategoryColor) -> CategoryColor:
        try:
            return CategoryColor(value)
        except ValueError:
            raise CategoryAppearanceInvalidError("color") from None

    @staticmethod
    def validate_icon(value: CategoryIcon) -> CategoryIcon:
        try:
            return CategoryIcon(value)
        except ValueError:
            raise CategoryAppearanceInvalidError("icon") from None

    @staticmethod
    def validate_update(command: CategoryUpdateCommand) -> None:
        if command.name is None and command.color is None and command.icon is None:
            raise RequestInvalidError({"request": ["At least one field is required."]})
