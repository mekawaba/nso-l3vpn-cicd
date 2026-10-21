*** Settings ***
Documentation     L3VPN service package CI test on NSO 6.7 container with IOS XR netsim.
...               No expected device config is kept here: the tests only check that
...               the service objects exist. Value-level review is done by the LLM.
Library           libraries/NsoCiLibrary.py    ${PARAMS_FILE}    ${ARTIFACTS_DIR}    AS    NSO
Suite Teardown    NSO.Collect Logs And Cleanup

*** Variables ***
${PARAMS_FILE}      ${CURDIR}/../params/service_params.yaml
${ARTIFACTS_DIR}    ${CURDIR}/../../artifacts
${PACKAGE_DIR}      ${CURDIR}/../../packages/l3vpn

*** Test Cases ***
01 NSOコンテナの起動
    NSO.Start NSO Container

02 Netsimデバイス(PE-1/PE-2)の起動とNSOへの登録
    NSO.Start Netsim Devices
    NSO.Register Netsim Devices

03 前提設定の投入 (Loopback0 / iBGP VPNv4)
    NSO.Apply Prerequisite Config

04 サービスパッケージのインストール
    NSO.Install Service Package    ${PACKAGE_DIR}

05 サービスの適用 (パラメータ投入と validate)
    NSO.Apply Service And Validate

06 Dry-run
    ${native}=    NSO.Dry Run Service
    @{devices}=    NSO.Get Device Names
    FOR    ${device}    IN    @{devices}
        Should Contain    ${native}    name ${device}
    END

07 Commit (pyATS で Commit 前のスナップショットを取得)
    @{devices}=    NSO.Get Device Names
    FOR    ${device}    IN    @{devices}
        NSO.Take Device Config Snapshot    ${device}    before
    END
    NSO.Commit Service
    NSO.Service Should Be In Sync

08 設定の確認 (pyATS で show running-config を取得し Genie で解析・差分)
    @{devices}=    NSO.Get Device Names
    FOR    ${device}    IN    @{devices}
        NSO.Take Device Config Snapshot    ${device}    after
        NSO.Device Config Should Contain Service Structure    ${device}
        NSO.Diff Device Config Snapshots    ${device}    before    after
    END
