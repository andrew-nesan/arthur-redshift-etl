#! /bin/bash

# This is the generic script to rule them all. We fall back to some old school shell hackery
# to use names of scripts to avoid passing in too many little silly parameters.

set -e -u

SCRIPT=`basename $0 .sh`
DW_VERB="${SCRIPT%%_*}"
DW_OBJECT="${SCRIPT#*_}"
DW_BASE_NAME="${DW_OBJECT//_/-}"

if [[ "$SCRIPT" = "do_cloudformation" ]]; then
    echo "You should be using one of the create, update, or delete scripts instead!"
    exit 1
fi

BINDIR=`dirname $0`
TEMPLATE_FILE="$BINDIR/$DW_OBJECT.yaml"
case "$TEMPLATE_FILE" in
    /*)
      TEMPLATE_URI="file://$TEMPLATE_FILE"
      ;;
    *)
      TEMPLATE_URI="file://./$TEMPLATE_FILE"
      ;;
esac

if [[ ! -r "$TEMPLATE_FILE" ]]; then
    echo "Cannot read $TEMPLATE_FILE -- you lost $DW_OBJECT.yaml?"
    exit 1
fi

if [[ $# -lt 1 || "$1" = "-h" ]]; then
    cat <<EOF
Usage: $0 ENV [Key=Value [Key=Value ...]]

Run $DW_VERB on $DW_OBJECT stack named "${DW_BASE_NAME}-{ENV}".
All parameters will be passed to AWS CLI after transformation to "ParameterKey=Key,ParameterValue=Value" syntax.
EOF
    exit 0
fi

ENV_NAME="$1"
STACK_NAME="${DW_BASE_NAME}-${ENV_NAME}"
shift 1

STACK_PARAMETERS=""
for KV in "$@"; do
    PARAMETER_KEY="${KV%%=*}"
    PARAMETER_VALUE="${KV#*=}"
    case "$PARAMETER_VALUE" in
        "UsePreviousValue")
          STACK_PARAMETERS="$STACK_PARAMETERS ParameterKey=$PARAMETER_KEY,UsePreviousValue=true"
          ;;
        *)
          STACK_PARAMETERS="$STACK_PARAMETERS ParameterKey=$PARAMETER_KEY,ParameterValue=$PARAMETER_VALUE"
          ;;
    esac
done

set -x
STACK_PARAMETERS="${STACK_PARAMETERS# }"

# Because of the "set -e", a failed validation will stop this script:
aws cloudformation validate-template --template-body "$TEMPLATE_URI" >/dev/null

case "$DW_VERB" in

  create)

    aws cloudformation create-stack \
        --stack-name "$STACK_NAME" \
        --template-body "$TEMPLATE_URI" \
        --on-failure DO_NOTHING \
        --capabilities CAPABILITY_NAMED_IAM \
        --parameters $STACK_PARAMETERS \
        --tags \
            "Key=user:project,Value=data-warehouse" \
            "Key=user:env,Value=$ENV_NAME"
    ;;

  update)

    aws cloudformation update-stack \
        --stack-name "$STACK_NAME" \
        --template-body "$TEMPLATE_URI" \
        --capabilities CAPABILITY_NAMED_IAM \
        --parameters $STACK_PARAMETERS \
        --tags \
            "Key=user:project,Value=data-warehouse" \
            "Key=user:env,Value=$ENV_NAME"
    ;;

  delete)

    aws cloudformation delete-stack \
        --stack-name "$STACK_NAME"
    ;;

   *)
    echo "Unexpected verb: $DW_VERB"
    exit 1
    ;;

esac

set +x
echo "To see resources for this stack, run:"
echo
echo "aws cloudformation list-stack-resources --stack-name \"$STACK_NAME\""