// Deal Desk — Azure infrastructure.
// App Service (Linux, Python) + Key Vault for secrets + managed identity.
// SQLite lives on /home, which App Service persists across restarts and deploys.

@description('Base name; everything is derived from this.')
param appName string = 'dealdesk'

@description('Region. Pick one close to the merchants you buy from — it shaves latency off checkout.')
param location string = resourceGroup().location

@description('B1 is enough for this. S1 if you want autoscale/slots.')
@allowed(['B1', 'B2', 'S1', 'P0v3', 'P1v3'])
param sku string = 'B1'

@secure()
param adminPasswordHash string
@secure()
param sessionSecret string
@secure()
param ingestToken string
@secure()
param workerToken string

param enableAppInsights bool = true

var suffix = uniqueString(resourceGroup().id)
var siteName = '${appName}-${suffix}'
var kvName = take('kv-${appName}-${suffix}', 24)

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${appName}-plan'
  location: location
  sku: { name: sku }
  kind: 'linux'
  properties: { reserved: true }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

resource secretAdmin 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'admin-password-hash'
  properties: { value: adminPasswordHash }
}
resource secretSession 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'session-secret'
  properties: { value: sessionSecret }
}
resource secretIngest 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'ingest-token'
  properties: { value: ingestToken }
}
resource secretWorker 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'worker-token'
  properties: { value: workerToken }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = if (enableAppInsights) {
  name: '${appName}-ai'
  location: location
  kind: 'web'
  properties: { Application_Type: 'web' }
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: siteName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'bash startup.sh'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      alwaysOn: true
      healthCheckPath: '/api/health'
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'ENABLE_ORYX_BUILD', value: 'true' }
        { name: 'WEBSITES_CONTAINER_START_TIME_LIMIT', value: '300' }
        { name: 'PYTHONUNBUFFERED', value: '1' }
        { name: 'DB_PATH', value: '/home/data/dealbot.db' }
        { name: 'ADMIN_USER', value: 'admin' }
        { name: 'ALLOW_NETWORK', value: '1' }
        { name: 'AUTO_BUY', value: '0' }
        { name: 'ADMIN_PASSWORD_HASH', value: '@Microsoft.KeyVault(SecretUri=${secretAdmin.properties.secretUri})' }
        { name: 'SESSION_SECRET', value: '@Microsoft.KeyVault(SecretUri=${secretSession.properties.secretUri})' }
        { name: 'INGEST_TOKEN', value: '@Microsoft.KeyVault(SecretUri=${secretIngest.properties.secretUri})' }
        { name: 'WORKER_TOKEN', value: '@Microsoft.KeyVault(SecretUri=${secretWorker.properties.secretUri})' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: enableAppInsights ? insights!.properties.ConnectionString : '' }
      ]
    }
  }
}

// Let the web app read its own secrets out of Key Vault.
resource kvReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, site.id, '4633458b-17de-408a-b874-0445c86b69e6')
  properties: {
    // Key Vault Secrets User
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output siteUrl string = 'https://${site.properties.defaultHostName}'
output siteName string = site.name
output keyVault string = vault.name
