import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";

export function AzureDevOpsTokenHelpAnchor() {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-secondary">
        {t(I18nKey.AZURE_DEVOPS$TOKEN_HELP_TEXT)}
      </p>
      <a
        href="https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate"
        target="_blank"
        rel="noreferrer"
        className="text-sm text-primary hover:underline"
      >
        {t(I18nKey.AZURE_DEVOPS$TOKEN_HELP_LINK)}
      </a>
    </div>
  );
}