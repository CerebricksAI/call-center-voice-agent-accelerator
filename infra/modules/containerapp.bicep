param location string
param environmentName string
param uniqueSuffix string
param tags object
param exists bool
param identityId string
param identityClientId string
param containerRegistryName string
param aiServicesEndpoint string
param modelDeploymentName string
param acsConnectionStringSecretUri string
param twilioAuthTokenSecretUri string = ''
param infobipApiKeySecretUri string = ''
param infobipApiBaseUrl string = ''
param genesysApiKeySecretUri string = ''
param logAnalyticsWorkspaceName string
param appInsightsConnectionString string = ''
@description('The name of the container image')
param imageName string = ''
param debugMode bool = false
@description('Enable zone redundancy for the Container App Environment')
param zoneRedundant bool = true

@description('Model for an additional (second) container app, e.g. gpt-realtime-mini. Empty = do not create a second app.')
param secondModelDeploymentName string = ''

@description('Caller speech-to-text model (Voice Live input transcription) for both apps. gpt-4o-transcribe is markedly more accurate on names/numbers (validated live); gpt-4o-mini-transcribe is cheaper; whisper-1 was the prior fallback.')
param transcriptionModel string = 'gpt-4o-transcribe'

// Helper to sanitize environmentName for valid container app name
var sanitizedEnvName = toLower(replace(replace(replace(environmentName, ' ', '-'), '--', '-'), '_', '-'))
var containerAppName = take('ca-${sanitizedEnvName}-${uniqueSuffix}', 32)
var containerAppRtName = take('ca-rt-${sanitizedEnvName}-${uniqueSuffix}', 32)
var containerEnvName = take('cae-${sanitizedEnvName}-${uniqueSuffix}', 32)

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' existing = { name: logAnalyticsWorkspaceName }


module fetchLatestImage './fetch-container-image.bicep' = {
  name: '${containerAppName}-fetch-image'
  params: {
    exists: exists
    name: containerAppName
  }
}

resource containerAppEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: containerEnvName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsWorkspace.properties.customerId
        sharedKey: logAnalyticsWorkspace.listKeys().primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: containerAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'app' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: '${containerRegistryName}.azurecr.io'
          identity: identityId
        }
      ]
      secrets: concat(
        !empty(acsConnectionStringSecretUri) ? [
          {
            name: 'acs-connection-string'
            keyVaultUrl: acsConnectionStringSecretUri
            identity: identityId
          }
        ] : [],
        !empty(twilioAuthTokenSecretUri) ? [
          {
            name: 'twilio-auth-token'
            keyVaultUrl: twilioAuthTokenSecretUri
          identity: identityId
        }
      ] : [],
        !empty(infobipApiKeySecretUri) ? [
          {
            name: 'infobip-api-key'
            keyVaultUrl: infobipApiKeySecretUri
            identity: identityId
          }
        ] : [],
        !empty(genesysApiKeySecretUri) ? [
          {
            name: 'genesys-api-key'
            keyVaultUrl: genesysApiKeySecretUri
            identity: identityId
          }
        ] : [])
    }
    template: {
      containers: [
        {
          name: 'main'
          image: !empty(imageName) ? imageName : 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          env: concat([
            {
              name: 'AZURE_VOICE_LIVE_ENDPOINT'
              value: aiServicesEndpoint
            }
            {
              name: 'AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID'
              value: identityClientId
            }
            {
              name: 'VOICE_LIVE_MODEL'
              value: modelDeploymentName
            }
            {
              name: 'INPUT_TRANSCRIPTION_MODEL'
              value: transcriptionModel
            }
            {
              name: 'AZD_SERVICE_NAME'
              value: 'app'
            }
            {
              name: 'CONTAINER_APP_NAME'
              value: containerAppName
            }
            {
              name: 'DEBUG_MODE'
              value: string(debugMode)
            }
            {
              name: 'ORCHESTRATOR_ENABLED'
              value: 'true'
            }
            {
              name: 'ORCHESTRATOR_ENGINE'
              value: 'langgraph'
            }
            {
              name: 'SEMANTIC_INTENT_ENABLED'
              value: 'true'
            }
          ], !empty(acsConnectionStringSecretUri) ? [
            {
              name: 'ACS_CONNECTION_STRING'
              secretRef: 'acs-connection-string'
            }
          ] : [], !empty(twilioAuthTokenSecretUri) ? [
            {
              name: 'TWILIO_AUTH_TOKEN'
              secretRef: 'twilio-auth-token'
            }
          ] : [], !empty(infobipApiKeySecretUri) ? [
            {
              name: 'INFOBIP_API_KEY'
              secretRef: 'infobip-api-key'
            }
            {
              name: 'INFOBIP_API_BASE_URL'
              value: infobipApiBaseUrl
            }
          ] : [], !empty(genesysApiKeySecretUri) ? [
            {
              name: 'GENESYS_API_KEY'
              secretRef: 'genesys-api-key'
            }
          ] : [])
          resources: {
            cpu: json('2.0')
            memory: '4.0Gi'
          }
        }
      ]
      // TODO add memory/cpu scaling
      scale: {
        minReplicas: 1
        maxReplicas: 10
        rules: [
          {
            name: 'http-scaler'
            http: {
              metadata: {
                concurrentRequests: '100'
              }
            }
          }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Second container app (additive) — same backend AI Services / Key Vault / identity,
// different Voice Live model. Created only when secondModelDeploymentName is set.
// Tagged azd-service-name 'app2' so the pipeline deploys it as a separate service.
// ---------------------------------------------------------------------------
resource containerAppRt 'Microsoft.App/containerApps@2024-10-02-preview' = if (!empty(secondModelDeploymentName)) {
  name: containerAppRtName
  location: location
  tags: union(tags, { 'azd-service-name': 'app2' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: '${containerRegistryName}.azurecr.io'
          identity: identityId
        }
      ]
      secrets: concat(
        !empty(acsConnectionStringSecretUri) ? [
          {
            name: 'acs-connection-string'
            keyVaultUrl: acsConnectionStringSecretUri
            identity: identityId
          }
        ] : [],
        !empty(twilioAuthTokenSecretUri) ? [
          {
            name: 'twilio-auth-token'
            keyVaultUrl: twilioAuthTokenSecretUri
            identity: identityId
          }
        ] : [],
        !empty(infobipApiKeySecretUri) ? [
          {
            name: 'infobip-api-key'
            keyVaultUrl: infobipApiKeySecretUri
            identity: identityId
          }
        ] : [],
        !empty(genesysApiKeySecretUri) ? [
          {
            name: 'genesys-api-key'
            keyVaultUrl: genesysApiKeySecretUri
            identity: identityId
          }
        ] : [])
    }
    template: {
      containers: [
        {
          name: 'main'
          image: !empty(imageName) ? imageName : 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          env: concat([
            {
              name: 'AZURE_VOICE_LIVE_ENDPOINT'
              value: aiServicesEndpoint
            }
            {
              name: 'AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID'
              value: identityClientId
            }
            {
              name: 'VOICE_LIVE_MODEL'
              value: secondModelDeploymentName
            }
            {
              name: 'INPUT_TRANSCRIPTION_MODEL'
              value: transcriptionModel
            }
            {
              name: 'AZD_SERVICE_NAME'
              value: 'app2'
            }
            {
              name: 'CONTAINER_APP_NAME'
              value: containerAppRtName
            }
            {
              name: 'EXTRACT_MODEL'
              value: 'gpt-4o-mini'
            }
            {
              name: 'SUMMARY_MODEL'
              value: 'gpt-4o-mini'
            }
            {
              name: 'VOICE_LIVE_EOU_THRESHOLD'
              value: 'medium'
            }
            {
              name: 'VOICE_LIVE_EOU_TIMEOUT_MS'
              value: '900'
            }
            {
              name: 'DEBUG_MODE'
              value: string(debugMode)
            }
            {
              name: 'ORCHESTRATOR_ENABLED'
              value: 'true'
            }
            {
              name: 'ORCHESTRATOR_ENGINE'
              value: 'langgraph'
            }
            {
              name: 'SEMANTIC_INTENT_ENABLED'
              value: 'true'
            }
          ], !empty(acsConnectionStringSecretUri) ? [
            {
              name: 'ACS_CONNECTION_STRING'
              secretRef: 'acs-connection-string'
            }
          ] : [], !empty(twilioAuthTokenSecretUri) ? [
            {
              name: 'TWILIO_AUTH_TOKEN'
              secretRef: 'twilio-auth-token'
            }
          ] : [], !empty(infobipApiKeySecretUri) ? [
            {
              name: 'INFOBIP_API_KEY'
              secretRef: 'infobip-api-key'
            }
            {
              name: 'INFOBIP_API_BASE_URL'
              value: infobipApiBaseUrl
            }
          ] : [], !empty(genesysApiKeySecretUri) ? [
            {
              name: 'GENESYS_API_KEY'
              secretRef: 'genesys-api-key'
            }
          ] : [])
          resources: {
            cpu: json('2.0')
            memory: '4.0Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 10
        rules: [
          {
            name: 'http-scaler'
            http: {
              metadata: {
                concurrentRequests: '100'
              }
            }
          }
        ]
      }
    }
  }
}

output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppId string = containerApp.id
