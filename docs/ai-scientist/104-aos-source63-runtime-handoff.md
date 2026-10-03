# AOS source63 ve ayrı runtime aktarımı

2026-10-01. Scientist `44f840bd23de4fd0a124d1f1be3a3b99ee22904d`,
AOS `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` ve aşağıda pinlenen yerel farklar.

## Son canlı readback: eski source63 pinleri artık eşleşmiyor

İlk source63 karşılaştırması aşağıdaki eski pinlerle geçti. Paralel AOS
oturumu native authenticator'a optional `deadline` desteği ekledi;12:15
Türkiye saati sonrası gerçek checkout yeniden kontrol edildi. Eski beklenen
pinlerle checker **exit2/source_ready=false**: selected source, tracked diff
ve selected untracked pin mismatch. Yeni gözlem onay değildir:

```text
observed_selected_source: 6f84e8796059005be3704f25093b232127cdeedc87c8afede568525be369ce8d
observed_tracked_diff: 6d6957874ba4f6667db6ac0f13bbc9f2dcfca0c9ce22c71cbcd2d10d196603cf
observed_selected_untracked: 5eb2a2624ce3c452255d87050d61d8deba73773266c9424a3dbaadf23643f91b
```

Scientist `om-stop` disabled planı eski AOS b56c… kaynağına bağlıdır;
**güncel runtime'a etkinleştirilemez**. AOS yeni source63 raporunu/canonical
servis manifestini aktarmalı; diff review ve consumer deadline bağlantısı
ardından yeni kapalı plan oluşturulur. Scientist beklenen hash'i gözlenenle
kendiliğinden değiştirmedi; servis/model/GPU başlatılmadı.

## Önceki kaynak karşılaştırması: geçen

Scientist source63 profilini ve native `SystemdCallerAuthenticator` kontrolünü
destekliyor. Gerçek checkout üzerinde 63/63 dosya eşleşti. Checker exit3:
`source_ready=true`, tek kalan neden
`joint_runtime_capability_confirmation_pending`. Önceki exit2, iki zorunlu
beklenen hash parametresi verilmediğindendi; kontrol gevşetilmedi.

```text
source_profile: lab_readback_history_candidate_v1
selected_source: b56c54a1bb63653f0bc69ad55caf25f8cce353ece97e8d7ceab80a640e03ca46
tracked_diff: 8de33b9870ef066e97befdf772ed96b1e399f1a63cf600cf2832d493c2a7e5ee
selected_untracked: 746543ad1d46a967f418ceeb576f2bf2f2e4f2bcc1d9e5618d05fab97c5769e0
```

Bu seçili kaynak kontrolüdür; tam deployment/bağımlılık kabulü değildir.
Actual worker dosyaları vardır; `shared-gpu-turns/lab-external` flag'leri
actual checkout'ta yoktur. Native startup hooks yolu kullanılmalıdır.

AOS oturumunun hazırladığı owner-only workspace ve GPU-disabled staging unit
salt okunur incelendi. Aktif AOS oturumuna veya AOS dosyalarına yazılmadı.
Scientist preparer yeni caller adına bağlı kapalı yapılandırmayı exit0 ile üretti:

```text
caller_unit: swapp-aos-gpu-joint-acceptance.service
configuration_directory: /home/cachyos/ai-scientist/data/runtime/native-config-source63-om-stop-20261001
profile_file_sha256: 81077a0b395a6207df22d16ff489b051df9b22bea1ef90a35246f745670b15cb
disabled_policy_sha256: 56ffc1d10f8e409ae140620a5a34ce27e120080723368c8e9bea148c5b208dbe
scientist_source_fingerprint: 100cd777a7e45c436b3bfca92b54c9410cf1f7b5b23d1a669e466556a4149217
```

Private source receipt:
`data/runtime/aos-source63-patch/actual-source63-reviewed-preflight.private.json`.
Config plan `plan.json`, profiles `profiles.json`, policy
`control-policy.disabled.json`. Üç profil: Decider turn, Bonsai recovery,
Bonsai vision; manifest/output pinleri plan içindedir. Yeni policy
`enabled=false`; servis/model/GPU başlatılmadı ve scheduler yaratılmadı.

**Canonical principal düzeltmesi:** AOS'un ilk staging unit adı
`swapp-aos-joint-acceptance.service`, scheduler'ın original
`swapp-aos-gpu-*.service` ad alanı dışında kaldığı için canlı capture tarafından
reddedilir. Önceki `native-config-source63-joint-20261001` planı bu yüzden
superseded'dır; oradaki528dbd… policy pin'i kullanılmaz. Scientist preparer
artık aynı canonical principal kontrolünü yayımdan önce uygular. AOS oturumu
kendi staging unit'ini yukarıdaki canonical ada ve mevcut `swapp-gpu.slice`
altına taşımalı; Scientist AOS unit dosyasını değiştirmedi. İki erken-reject
regresyonu eski kodda başarısız, düzeltmede başarılıdır. Canonical scheduler
ve kimlik denetimi gevşetilmedi.

## Runtime bağlantısı ve kalan girdiler

`scripts/aos_configured_runtime_factory.py` içindeki
`ConfiguredScientistAdmissionFactory` AOS Python'unda kullanılmalıdır.
`serve_desktop.main` dört açık hook alır:

```python
scientist_admission_factory=factory
scientist_bootstrap_expected_peer=factory.expected_peer
scientist_confirm_runtime=factory.confirm_runtime
scientist_output_contract=factory.output_contract
```

Wrapper, concrete native factory gerektiren `scientist_bootstrap_factory`
shortcut'ına verilmez. AOS-owned staging unit'in `--engine disabled` komutu
native kabul değildir. Native launch için bu hook'ları kuran entrypoint,
`--engine scientist`, canonical broker socket ve gözden geçirilmiş enabled
policy/profile/artifact/source/current-rights girdileri gerekir. Disabled policy
hash'i enabled policy hash'i olarak kullanılamaz.

Canlı bindings service başladıktan sonra exact aynı AOS MainPID/start/boot/
invocation/cgroup'dan alınmalı; statik veya eski nesil binding kopyalanmaz.
Full artifact receipt ve current-rights doğrulaması olmadan factory reddeder.
Sadece manifest/env envanter hash'leri bu girdilerin yerine geçmez.
Lab task aktarımı ayrıca mevcut trusted Lab startup config/capability doğrulamasını
ve yetkili sonlu görev ile bağımsız sonuç oracle'ını gerektirir.

Somut aynı-process launch adayı Scientist private staging'de hazırlanmıştır:
`data/runtime/aos-source63-patch/launch-composition/launch_native.py`, hash
`ecd9a7b49a70991bee65f16a15488232e42d03224b385606b896aebd6de71b87`.
Actual AOS Python'unda import/compile ve Ruff geçti; servis başlatılmadı.
Aynı dizindeki `REVIEW.md` exact PYTHONPATH/ExecStart ve launch input alanlarını
tanımlar. Current-rights adayı gerçek pinned enabled policy/source/profile ve
native caller/broker generation kontrolünü yapar; permissive callback yoktur.
Private joint Lab capability/readback hooks hazırlanmış ve original AOS
typed task/action CPU fixture'larıyla20 kontrolü geçmiştir. Authenticated
capability endpoint Scientist kaynaklarına uygulandı;8 real create_app
CPU kontrolü auth/owner/manifest pinlerini doğruladı. Native launcher ve
callback halen private adayıdır. **Etkinleştirme öncesi kalan:** native
sabit auth timeout'larının toplam deadline'a uyumu, bağımsız static artifact
input, canlı API generation ve reviewed joint capability, enabled config. Bu aday onaylı runtime komutu değildir; AOS
staging unit'e şimdilik kurulmaz. Native receipt300s geçerlidir; uzun iş için
stale receipt kabul edilmez.

AOS tarafındaki dar beklenti: `SystemdBrokerAuthenticator.authenticate` /
`still_current` ve `SystemdCallerAuthenticator.authenticate` kontrollerinin
mevcut bounded subprocess'leri, çağıranın kalan toplam deadline'ını da
koruyabilmeli. Bugünkü her çağrının bağımsız2s bütçesi ardışık kontrollerde
toplam4s kabul penceresini aşabilir; sonra fail-closed reddetmek hard-total
latency kanıtı değildir. Scientist bu AOS dosyalarını değiştirmedi. İki
oturum aynı optional outer-deadline API'sinde uzlaşmadan adayı etkinleştirmeyin.

Inference wire1, native bootstrap `aos-scientist-control.v1`, Lab broker
metadata `aos-scientist-control-contract.v2`, retained evidence-v3 ayrı
katmanlar olarak korunur. Tek tahsis DB'si
`~/.local/state/swapp-gpu/arbiter.sqlite3`; ikinci authority oluşturulmaz.
ACK/idle/quiesce GPU release kanıtı değildir. Cleanup belirsizse quarantine
korunur. GPU kabulünü yalnız Scientist yürütür.

## Çalıştırılmayan kabul

Enabled policy/runtime composition, gerçek AOS görevi, AOS→Scientist GPU devri,
Scientist araştırma sonucu AOS doğrulaması, birlikte ilerleme/fairness ve
kontrollü iptal/toparlanma henüz kabul edilmedi. Bu kaynak/config teslimi
gerçek GPU veya öğrenilmiş iyileşme olarak sunulmaz. Lisans/genel CI ayrıdır.

## AOS oturumuna kısa çıktı

Scientist source63 ve exact native caller düzeltmesi `44f840b` üzerinde hazır;
actual source preflight geçti. Ayrı AOS staging unit adına kapalı policy/config
hazırlandı; yukarıdaki hash'leri gözden geçirin. Native entrypoint dört mevcut
factory hook'unu kullanmalı; enabled policy'nin yeni hash'i ve same-process
current generation/artifact/current-rights doğrulaması birlikte provision
edilmeli. Staging unit'i GPU kabulü diye başlatmayın. AOS workspace/runtime
sahipliği sizde, tek entegre GPU yürütücüsü Scientist'tir.

Güncel unit adı **swapp-aos-gpu-joint-acceptance.service** olmalıdır; önceki
swapp-aos-joint-acceptance.service native principal kabulünden geçmez.

## Source63 raporuna güncel cevap

Kullanıcının ilettiği source63-report.json actual checkout pinleriyle
`reviewed_snapshot` olarak tekrar karşılaştırıldı: checker exit3,
source_ready=true; tek engel joint runtime capability confirmation. Dirty
checkout/untracked kaynakları public temiz ana dal diye sunmuyoruz.
AOS raporu SHA256:
`6b51e586a819b79a243fe2b628d26418187b470017344c67e56ef0560143c97d`.
Native factory caller bağlantısı Scientist kaynaklarında mevcut. İlk AOS servis
manifesti old unit/engine disabled, native_factory_wired=false durumundaydı;
09:19 güncellemesi aşağıda.
Manifest hash kontrolü tam weights/dependencies/current-rights kabulü değil.

Scientist'in yeni stop observer kaynakları native_runtime hash'ini değiştirdi.
Bu yüzden yukarıdaki **om-stop** kapalı plan yeniden üretildi; önceki
gpu-joint dizinindeki5e334b… policy superseded. enabled=false korunuyor.
Profile pin'i aynı; yeni Scientist source100cd777… ve disabled policy56ffc1…
AOS kendi canonical unit + Slice ve kalan toplam deadline desteğini sağlamalı.
Enabled policy farklı bir hash gerektirir; kapalı plan hash'ini onay diye
kullanmayın. Aynı scheduler + physical cleanup/quarantine değişmedi.

Tek-öneri standalone araştırma terminal/holdout/rapor/cleanup doğrulandı:
[doğrulanmış sonuç](105-first-completed-local-research.md). Bu AOS native
kabulü değildir; entegre GPU koşusu başlatılmadı.

## AOS paralel düzeltme gözlemi — 09:19 UTC

Actual runtime-config-manifest artık canonical
`swapp-aos-gpu-joint-acceptance.service`, `Slice=swapp-gpu.slice`
gösteriyor. Servis installed=false/started=false/native_factory_wired=false;
GPU ve admission yine kapalı. Bu iki staging adlandırma şartı karşılandı.
Eski source63-report.json hâlâ b56c… pinlerini taşıyor; güncel selected source
raporu gerekir. Scientist manifest gözlemini source approval yerine koymaz.

Actual AOS authenticator API'si artık `deadline=None` kabul ediyor; clock
**absolute time.monotonic**. Her çağrının2s tavanı korunur ve aynı outer
deadline kalan bütçeyi sınırlar. Native API/caller/broker + ortak3,5s
CPU fixture'ları27 passed; AOS import `-B`, dosya/servis değişikliği yok.
Private tüketici uyarlaması
`data/runtime/aos-source63-patch/outer-deadline/call-sites.patch`;
launcher'ın3,5s penceresini yenilemeden aktarır. Halen uygulanmadı.
Configured/native factory ve AOS transport client çağrıları da aynı deadline'ı
aktarmalı; yalnız API keyword'ünün mevcut olması uçtan uca toplam süre
kanıtı değildir. Fiziksel cleanup/quarantine şartları korunur.

AOS oturumuna güncel kısa aktarım: canonical unit/Slice hazırlığı görüldü;
source63 raporunu yeni source pinleriyle yenileyin. Native auth optional
deadline kabul ediyor; Scientist tüketici patch'i review için hazır.
Configured factory4s/current-rights3,5s ve transport request deadline'ı
monotonic clock'ta aynı bütçeyle native authenticate/still_current/caller
çağrılarına aktarılmalı. Enabled config/full artifact/current generation ve
joint capability henüz kabul değil; yalnız Scientist GPU koşusunu başlatır.


## 2026-10-01 gerçek OM ve stop sonucu

[Gerçek çalışma modu ve stop kanıtı](106-mode-agent-and-inflight-stop.md):
947fe42f terminal completed, tek LSH önerisi DISCARD; holdout manual_review.
7038f5fa stop ACK ve exact fiziksel cleanup geçti, SQL terminal/rapor başarısız.
Stop kabulü açık; exit0 tamamlanma kanıtı değildir. AOS oturumuyla doğrudan
mesajlaşma kuruldu; source verifier shared absolute monotonic deadline uyumu
Scientist66 CPU testiyle doğrulandı. Güncel AOS selected source cc62d495…;
source_ready=true/admission=false. Eski b56/619b kaynak planları superseded.
Native staging GPU/admission kapalı; gerçek AOS kabulü çalıştırılmadı.
