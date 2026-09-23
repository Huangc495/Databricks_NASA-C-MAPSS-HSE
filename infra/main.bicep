targetScope = 'resourceGroup'

param location string = resourceGroup().location
param workspaceName string = 'dbw-sentinelops-dev'
param storageName string = 'stsent${uniqueString(resourceGroup().id)}'
@allowed(['premium', 'trial'])
param workspaceSku string = 'premium'

var tags = { project: 'SentinelOps', environment: 'dev' }

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    isHnsEnabled: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
  }
}
resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}
resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for name in ['landing', 'bronze', 'silver', 'gold', 'metastore']: {
  parent: blob
  name: name
  properties: { publicAccess: 'None' }
}]
resource connector 'Microsoft.Databricks/accessConnectors@2023-05-01' = {
  name: 'ac-sentinelops-dev'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {}
}
resource blobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, connector.id, 'blob-contributor')
  scope: storage
  properties: {
    principalId: connector.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}
resource workspace 'Microsoft.Databricks/workspaces@2024-05-01' = {
  name: workspaceName
  location: location
  tags: tags
  sku: { name: workspaceSku }
  properties: {
    managedResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups', '${resourceGroup().name}-managed')
    publicNetworkAccess: 'Enabled'
    parameters: { enableNoPublicIp: { value: true } }
  }
}
output workspaceUrl string = workspace.properties.workspaceUrl
output workspaceResourceId string = workspace.id
output accessConnectorId string = connector.id
output storageAccountName string = storage.name
