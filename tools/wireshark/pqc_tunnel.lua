-- Minimal skeleton dissector (header-only) for dev convenience.
local p = Proto("pqctun","PQC Tunnel")
local f_version = ProtoField.uint8("pqctun.version","version", base.DEC)
local f_kem_id  = ProtoField.uint8("pqctun.kem_id","kem_id", base.DEC)
local f_kem_prm = ProtoField.uint8("pqctun.kem_param","kem_param", base.DEC)
local f_sig_id  = ProtoField.uint8("pqctun.sig_id","sig_id", base.DEC)
local f_sig_prm = ProtoField.uint8("pqctun.sig_param","sig_param", base.DEC)
local f_sid     = ProtoField.bytes("pqctun.session_id","session_id")
local f_seq     = ProtoField.uint64("pqctun.seq","seq", base.DEC)
local f_epoch   = ProtoField.uint8("pqctun.epoch","epoch", base.DEC)
p.fields = {f_version,f_kem_id,f_kem_prm,f_sig_id,f_sig_prm,f_sid,f_seq,f_epoch}
function p.dissector(buf,pkt,tree)
  if buf:len() < 1+1+1+1+1+8+8+1 then return end
  local t = tree:add(p, buf(0))
  local o=0
  t:add(f_version, buf(o,1)); o=o+1
  t:add(f_kem_id,  buf(o,1)); o=o+1
  t:add(f_kem_prm, buf(o,1)); o=o+1
  t:add(f_sig_id,  buf(o,1)); o=o+1
  t:add(f_sig_prm, buf(o,1)); o=o+1
  t:add(f_sid,     buf(o,8)); o=o+8
  t:add(f_seq,     buf(o,8)); o=o+8
  t:add(f_epoch,   buf(o,1)); o=o+1
end
local udp_table = DissectorTable.get("udp.port")
-- you can: udp_table:add(5810, p) etc.
