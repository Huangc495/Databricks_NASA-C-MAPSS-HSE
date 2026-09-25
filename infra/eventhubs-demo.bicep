// Bounded Event Hubs demo (task B). Billable while it exists: delete the namespace the same day.
//   az deployment group what-if --resource-group rg-sentinelops-dev --template-file infra/eventhubs-demo.bicep
//   az deployment group create  --resource-group rg-sentinelops-dev --template-file infra/eventhubs-demo.bicep
//   az eventhubs namespace delete --resource-group rg-sentinelops-dev --name evhns-sentinelops-7s5fwy
// Standard is the lowest tier with the Kafka endpoint. SAS policies are namespace-level and
// single-purpose: the pipeline gets listen only, the local producer send only.
param location string = resourceGroup().location
param namespaceName string = 'evhns-sentinelops-7s5fwy'
param hubName string = 'cmapss-telemetry'

resource namespace 'Microsoft.EventHub/namespaces@2026-01-01' = {
  name: namespaceName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Standard'
    capacity: 1
  }
  tags: {
    project: 'sentinelops'
    purpose: 'bounded-demo'
  }
  properties: {
    minimumTlsVersion: '1.2'
    isAutoInflateEnabled: false
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false // SAS keys: the Kafka consumer and the REST producer both use them.
  }
}

resource hub 'Microsoft.EventHub/namespaces/eventhubs@2026-01-01' = {
  parent: namespace
  name: hubName
  properties: {
    partitionCount: 2
    messageRetentionInDays: 1
  }
}

resource listen 'Microsoft.EventHub/namespaces/authorizationRules@2026-01-01' = {
  parent: namespace
  name: 'cmapss-listen'
  properties: {
    rights: [
      'Listen'
    ]
  }
}

resource send 'Microsoft.EventHub/namespaces/authorizationRules@2026-01-01' = {
  parent: namespace
  name: 'cmapss-send'
  properties: {
    rights: [
      'Send'
    ]
  }
}

output namespace string = namespace.name
output hub string = hub.name
