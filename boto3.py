import boto3

sts = boto3.client("sts")

response = sts.get_caller_identity()

print("Account:", response["Account"])
print("UserId:", response["UserId"])
print("Arn:", response["Arn"])
