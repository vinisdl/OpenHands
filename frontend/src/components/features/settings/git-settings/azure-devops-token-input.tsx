import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import { SettingsInput } from "../settings-input";
import { AzureDevOpsTokenHelpAnchor } from "./azure-devops-token-help-anchor";
import { KeyStatusIcon } from "../key-status-icon";

interface AzureDevOpsTokenInputProps {
  onChange: (value: string) => void;
  onOrganizationChange: (value: string) => void;
  onProjectChange: (value: string) => void;
  isAzureDevOpsTokenSet: boolean;
  name: string;
  organizationSet: string | null | undefined;
  projectSet: string | null | undefined;
}

export function AzureDevOpsTokenInput({
  onChange,
  onOrganizationChange,
  onProjectChange,
  isAzureDevOpsTokenSet,
  name,
  organizationSet,
  projectSet,
}: AzureDevOpsTokenInputProps) {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col gap-6">
      <SettingsInput
        testId={name}
        name={name}
        onChange={onChange}
        label={t(I18nKey.AZURE_DEVOPS$TOKEN_LABEL)}
        type="password"
        className="w-[680px]"
        placeholder={isAzureDevOpsTokenSet ? "<hidden>" : ""}
        startContent={
          isAzureDevOpsTokenSet && (
            <KeyStatusIcon
              testId="ado-set-token-indicator"
              isSet={isAzureDevOpsTokenSet}
            />
          )
        }
      />

      <SettingsInput
        onChange={onOrganizationChange || (() => {})}
        name="azure-devops-organization-input"
        testId="azure-devops-organization-input"
        label={t(I18nKey.AZURE_DEVOPS$ORGANIZATION_LABEL)}
        type="text"
        className="w-[680px]"
        placeholder="your-organization"
        defaultValue={organizationSet || undefined}
        startContent={
          organizationSet &&
          organizationSet.trim() !== "" && (
            <KeyStatusIcon testId="ado-set-organization-indicator" isSet />
          )
        }
      />

      <SettingsInput
        onChange={onProjectChange || (() => {})}
        name="azure-devops-project-input"
        testId="azure-devops-project-input"
        label={t(I18nKey.AZURE_DEVOPS$PROJECT_LABEL)}
        type="text"
        className="w-[680px]"
        placeholder="your-project"
        defaultValue={projectSet || undefined}
        startContent={
          projectSet &&
          projectSet.trim() !== "" && (
            <KeyStatusIcon testId="ado-set-project-indicator" isSet />
          )
        }
      />

      <AzureDevOpsTokenHelpAnchor />
    </div>
  );
}