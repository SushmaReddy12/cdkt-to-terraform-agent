#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { StorageStack } from '../lib/storage-stack';

const app = new cdk.App();
const env = { region: 'us-east-1' };
const environment = app.node.tryGetContext('environment') ?? 'dev';

new StorageStack(app, 'StorageStack', { env, environment });

cdk.Tags.of(app).add('project', 'sample-app');
