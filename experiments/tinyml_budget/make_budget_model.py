import numpy as np, tensorflow as tf
np.random.seed(0); tf.random.set_seed(0)
CONTEXT, CH, HORIZON = 288, 2, 36
def build(f1,f2,f3,d):
    inp=tf.keras.Input((CONTEXT,CH), batch_size=1)
    x=tf.keras.layers.Conv1D(f1,5,strides=2,padding="same",activation="relu")(inp)
    x=tf.keras.layers.Conv1D(f2,5,strides=2,padding="same",activation="relu")(x)
    x=tf.keras.layers.Conv1D(f3,5,strides=2,padding="same",activation="relu")(x)
    x=tf.keras.layers.Reshape((36*f3,))(x)
    x=tf.keras.layers.Dense(d,activation="relu")(x)
    out=tf.keras.layers.Dense(HORIZON)(x)
    return tf.keras.Model(inp,out)
def convert(m):
    def rep():
        for _ in range(100):
            yield [np.random.rand(1,CONTEXT,CH).astype(np.float32)]
    c=tf.lite.TFLiteConverter.from_keras_model(m)
    c.optimizations=[tf.lite.Optimize.DEFAULT]
    c.representative_dataset=rep
    c.target_spec.supported_ops=[tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    c.inference_input_type=tf.int8; c.inference_output_type=tf.int8
    return c.convert()
import sys
f1,f2,f3,d=[int(a) for a in sys.argv[1:5]]
m=build(f1,f2,f3,d); b=convert(m)
print("params",m.count_params(),"tflite bytes",len(b))
open("model.tflite","wb").write(b)
interp=tf.lite.Interpreter(model_content=b)
ops=sorted({o['op_name'] for o in interp._get_ops_details()})
print("ops",ops)
