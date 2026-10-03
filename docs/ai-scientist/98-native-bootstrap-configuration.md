# Native AOS bootstrap factory ve broker yapılandırması

2026-10-01. Scientist tabanı `ce1d902784de142d1f73a2ac238d48472627925a`;
actual AOS `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` ve incelenen yerel farkları.

## Uygulanan bağlantı

`scripts/aos_native_admission_factory.py` mevcut AOS desktop factory hook'una
bağlanır. Aynı Controller/Store üzerinde native `ScientistBootstrapCapture`
ve history2.0 kurar. İlk controller/session/runtime/owner/lease/generation bağı
korunur; pause, takeover ve generation değişikliği eski factory'yi yetkilendirmez.
Beklenen complete admission binding bağımsız trusted reader'dan gelir; ACK
kendi expected source/policy/profile/caller haklarını oluşturamaz. Current
source/runtime/revoke denetimi ve before-send durable writer zorunludur.

Caller generation içindeki `parent_pid` unit MainPID'sidir; kernel PPID değildir.
Caller MainPID veya aynı service cgroup altındaki child olabilir. PID/start/boot/
cgroup ve systemd invocation doğrulanır. Host callback transaction ownership'i
korumalıdır. Ağ çağrısı ve capability capture'ın SQL sınırlarını mevcut AOS
native sınıfları yönetir. Trusted host bağlantısı:

```python
factory = NativeScientistAdmissionFactory(
    control_socket,
    expected_binding=independent_binding_reader,
    verify_current=current_source_runtime_verifier,
    persist_bootstrap_intent=durable_writer,
)
serve_desktop.main(
    scientist_admission_factory=factory,
    scientist_bootstrap_expected_peer=factory.expected_peer,
    scientist_confirm_runtime=runtime_verifier,
    scientist_output_contract=pinned_output_contract,
)
```

Bu sağlayıcıları trusted host sağlamalıdır; factory constructor yetki vermez.

## Gerçek kaynak ve model yapılandırması

Scientist preflight AOS retained-host55, physical56, bootstrap57 ve provider58
profillerini tanır. Bootstrap/provider için actual async `prepare_async` ve
desktop `prepare_infer` zorunludur. Eksik/sync/eski API işe başlamadan reddedilir.
Exact reviewed snapshot pinleri ve runtime-admission-denied davranışı korunur.

`scripts/prepare_aos_native_configuration.py` reviewed native58 receipt'ten
Decider, Bonsai recovery ve Bonsai vision profilleri üretir. Manifest/deployment/
interpreter/artifact roots, output-v2 ve source pinleri mevcut `load_profiles`,
`ProfileRegistry.get`, `_model_paths` ve `ControlPolicy` ile doğrulanır. Bonsai
runtime kökü manifest `runtime_path` alanıdır; output bundle hash'i canonical
bundle'dan hesaplanır.180s activation/30s inference/210s total/300s queue
bütçeleri seçildi; bunlar performans ölçümü değildir. Bonsai mevcut manifest'in
context16384, temperature0 ve max_output512 ayarlarını korur.

Host'ta gerçek kaynak/manifestlerle özel `data/runtime/native-config-1` altında
profiles, disabled policy ve plan üretildi; exit0. Enabled policy yok; servis,
scheduler/DB/GPU oluşturulmadı, model indirilmedi. Full weight bytes ve interpreter
dependency kapanışı doğrulanmadı. Bu çıktı üretim admission veya araştırma kabulü
değildir.

## Geçen / kalan / çalıştırılmayan

- **Geçen:**133 odaklı kontrol; source frozen/exit0/7,622 saniye. Actual AOS
  interpreter gerçek Controller/private Store/history2.0/BootstrapCapture API'sini
  birleştirdi; pause sonrası factory reddetti. Runtime sentetik CPU, capability
  exchange yok; trust callbacks deny idi.
- **Kalan:** independently provisioned expected binding, current source/config
  ve principal sağlayıcısı, durable bootstrap writer, native launcher bağlantısı.
  Canonical broker/scheduler DB son denetimde mevcut değildi. KWin Wayland GPU
  baseline süreci görüldü; durdurulmadı. Bu gözlem ilerideki koşu için rezervasyon
  veya boşluk garantisi değildir.
- **Çalıştırılmayan:** gerçek model/Scorer, GPU tahsis/devir/fairness, allocated
  cancellation/recovery, full desktop kabulü, eğitim/öğrenilmiş adaptör.

Kabul **11 geçti / 7 kısmi / 4 açık**. AOS dosyaları/canlı servisleri değiştirilmedi;
push/merge/deploy yok. Lisans ve genel CI ayrı kalemlerdir.

## AOS oturumuna aktarım

Scientist native58 pinleri/async bootstrap bağlantısını tanıyor; controller-bound
factory ve üç profilin disabled config planı hazır. Yeni AOS source report'taki
`bootstrap_factory_candidate_v1` daha yeni bir öneridir. İcra sırasında factory
dosyası yoktu; kalite kapısı sırasında AOS oturumu dosyayı ekledi. Yeni native
factory kendi durable audit writer'ını da içeriyor; bu teslim yeni59 profilini
doğrulanmış saymaz. Sonraki bağlantıda aynı trusted callback sınırları korunarak
bu native factory kullanılabilir. Sabit güncel kaynak
receipt'i paylaşılmalı; GPU kabulünün tek yürütücüsü Scientist oturumudur.

[Source/config/API ve yerel fark hash'leri](review-evidence/native-bootstrap-configuration.json).

Zorunlu kalite kapısı **1987 passed / 7 opt-in skipped / 120 GPU-live deselected**;
yedi komut exit0, parent191,853687100 saniye. Python kaynakları değişmedi;
wheel build ve import kontrolü geçti.
