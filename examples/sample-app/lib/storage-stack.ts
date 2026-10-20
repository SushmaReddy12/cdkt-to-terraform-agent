import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';

export interface StorageStackProps extends cdk.StackProps {
  environment: string;
}

export class StorageStack extends cdk.Stack {
  public readonly table: dynamodb.Table;
  public readonly uploadsBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);

    this.uploadsBucket = new s3.Bucket(this, 'UploadsBucket', {
      bucketName: `sample-app-uploads-${props.environment}`,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Explicit bucket policy: deny any request that isn't over TLS.
    this.uploadsBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'DenyInsecureTransport',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ['s3:*'],
        resources: [this.uploadsBucket.bucketArn, this.uploadsBucket.arnForObjects('*')],
        conditions: { Bool: { 'aws:SecureTransport': 'false' } },
      })
    );

    this.table = new dynamodb.Table(this, 'ItemsTable', {
      tableName: `sample-app-items-${props.environment}`,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
    });

    new cdk.CfnOutput(this, 'TableName', { value: this.table.tableName });
    new cdk.CfnOutput(this, 'BucketName', { value: this.uploadsBucket.bucketName });
  }
}
